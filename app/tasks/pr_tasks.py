import asyncio
import random
import structlog
import uuid
import hashlib
from datetime import datetime, timedelta
from billiard.exceptions import SoftTimeLimitExceeded
from celery.exceptions import Retry
from sqlalchemy import and_, or_, select, update

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.failed_pr import FailedPR
from app.models.processed_pr import ProcessedPR
from app.services.github_client import fetch_pr_diff, fetch_compare_diff, fetch_pr_state
from app.services.github_comment import post_pr_comment
from app.services.retrieval import retrieve_for_hunks, split_diff_into_hunks
from app.services.synthesizer import GroqSynthesisUnavailable, synthesize_feedback
from app.services.llm_client import DEFAULT_MODEL, GroqRateLimitedError, GroqPermanentError
from app.services.review_quality import (
    changed_context_by_file,
    cluster_repeated_candidate_groups,
    deduplicate_findings,
    format_change_overview,
    historical_candidate_rank,
    remove_context_contradicted_findings,
    summarize_diff,
)

logger = structlog.get_logger()

BATCH_SIZE = 10


async def _release_closed_pr_claim(owner: str, repo_name: str, pr_number: int, head_sha: str) -> None:
    """Leave a durable closed state so a later reopen webhook can reset it."""
    async with AsyncSessionLocal() as session:
        await session.execute(update(ProcessedPR).where(
            ProcessedPR.repo_owner == owner,
            ProcessedPR.repo_name == repo_name,
            ProcessedPR.pr_number == pr_number,
            ProcessedPR.head_sha == head_sha,
            ProcessedPR.status == "processing",
        ).values(
            status="skipped_closed",
            task_id=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error=None,
        ))
        await session.commit()


async def _acquire_review_claim(owner, repo_name, pr_number, head_sha, task_id, lease_owner, redelivered):
    now = datetime.utcnow()
    claimable = or_(
        ProcessedPR.status.in_(("queued", "expired", "abandoned")),
        and_(ProcessedPR.status == "processing", ProcessedPR.lease_expires_at <= now),
        and_(ProcessedPR.status == "processing", ProcessedPR.task_id == task_id, redelivered),
    )
    stmt = update(ProcessedPR).where(
        ProcessedPR.repo_owner == owner,
        ProcessedPR.repo_name == repo_name,
        ProcessedPR.pr_number == pr_number,
        ProcessedPR.head_sha == head_sha,
        claimable,
        ProcessedPR.attempts < settings.review_max_attempts,
    ).values(
        status="processing",
        task_id=task_id,
        lease_owner=lease_owner,
        lease_expires_at=now + timedelta(seconds=settings.review_claim_lease_seconds),
        attempts=ProcessedPR.attempts + 1,
        last_error=None,
    ).returning(ProcessedPR.attempts, ProcessedPR.review_payload)
    async with AsyncSessionLocal() as session:
        result = await session.execute(stmt)
        row = result.first()
        await session.commit()
    return {"attempts": row.attempts, "review_payload": row.review_payload} if row else None


async def _save_review_checkpoint(owner, repo_name, pr_number, head_sha, lease_owner, payload):
    async with AsyncSessionLocal() as session:
        result = await session.execute(update(ProcessedPR).where(
            ProcessedPR.repo_owner == owner,
            ProcessedPR.repo_name == repo_name,
            ProcessedPR.pr_number == pr_number,
            ProcessedPR.head_sha == head_sha,
            ProcessedPR.status == "processing",
            ProcessedPR.lease_owner == lease_owner,
        ).values(review_payload=payload))
        await session.commit()
    if result.rowcount != 1:
        raise RuntimeError("review claim ownership changed before checkpoint")


async def _assert_review_claim(owner, repo_name, pr_number, head_sha, lease_owner):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ProcessedPR.id).where(
            ProcessedPR.repo_owner == owner,
            ProcessedPR.repo_name == repo_name,
            ProcessedPR.pr_number == pr_number,
            ProcessedPR.head_sha == head_sha,
            ProcessedPR.status == "processing",
            ProcessedPR.lease_owner == lease_owner,
            ProcessedPR.lease_expires_at > datetime.utcnow(),
        ))
        owns_claim = result.first() is not None
    if not owns_claim:
        raise RuntimeError("review claim expired or was fenced by another worker")


async def _finish_review_claim(owner, repo_name, pr_number, head_sha, lease_owner, status, error=None):
    async with AsyncSessionLocal() as session:
        result = await session.execute(update(ProcessedPR).where(
            ProcessedPR.repo_owner == owner,
            ProcessedPR.repo_name == repo_name,
            ProcessedPR.pr_number == pr_number,
            ProcessedPR.head_sha == head_sha,
            ProcessedPR.status == "processing",
            ProcessedPR.lease_owner == lease_owner,
        ).values(
            status=status,
            task_id=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error=error[:2000] if error else None,
        ))
        await session.commit()
    return result.rowcount == 1


async def _release_claim_for_retry(owner, repo_name, pr_number, head_sha, lease_owner):
    async with AsyncSessionLocal() as session:
        await session.execute(update(ProcessedPR).where(
            ProcessedPR.repo_owner == owner,
            ProcessedPR.repo_name == repo_name,
            ProcessedPR.pr_number == pr_number,
            ProcessedPR.head_sha == head_sha,
            ProcessedPR.status == "processing",
            ProcessedPR.lease_owner == lease_owner,
        ).values(
            status="queued",
            task_id=None,
            lease_owner=None,
            lease_expires_at=None,
            dispatched_at=datetime.utcnow(),
        ))
        await session.commit()


async def _record_claim_failure(owner, repo_name, pr_number, head_sha, lease_owner, error, attempts):
    async with AsyncSessionLocal() as session:
        result = await session.execute(update(ProcessedPR).where(
            ProcessedPR.repo_owner == owner,
            ProcessedPR.repo_name == repo_name,
            ProcessedPR.pr_number == pr_number,
            ProcessedPR.head_sha == head_sha,
            ProcessedPR.status == "processing",
            ProcessedPR.lease_owner == lease_owner,
        ).values(
            status="failed",
            task_id=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error=error[:2000],
        ))
        if result.rowcount == 1:
            session.add(FailedPR(
                repo_owner=owner,
                repo_name=repo_name,
                pr_number=pr_number,
                error=error[:2000],
                attempts=attempts,
            ))
        await session.commit()


async def _process_pr_async(
    pr_number: int,
    repo_name: str,
    owner: str,
    expected_head_sha: str,
    before_sha: str = None,
    after_sha: str = None,
    cached_review: dict = None,
    save_checkpoint=None,
    assert_claim=None,
    claim_attempt: int = None,
) -> dict:
    pr_state = await fetch_pr_state(owner, repo_name, pr_number)
    current_sha = pr_state["head_sha"]
    if cached_review is not None and cached_review.get("head_sha") != current_sha:
        cached_review = None
    if pr_state["state"] != "open":
        if expected_head_sha:
            await _release_closed_pr_claim(owner, repo_name, pr_number, expected_head_sha)
        logger.info("closed_pr_review_skipped", pr_number=pr_number, repo=repo_name, state=pr_state["state"])
        return {"status": "closed", "pr_number": pr_number, "head_sha": current_sha}
    if expected_head_sha and current_sha != expected_head_sha:
        logger.info(
            "obsolete_pr_review_skipped",
            pr_number=pr_number,
            repo=repo_name,
            queued_head_sha=expected_head_sha,
            current_head_sha=current_sha,
        )
        return {"status": "obsolete", "pr_number": pr_number}

    if cached_review is not None and cached_review.get("complete", True):
        feedback = cached_review["feedback"]
        similar_count = cached_review["similar_count"]
        hunks_scanned = cached_review["hunks_scanned"]
        synthesis_calls = cached_review["synthesis_calls"]
        partial = cached_review["partial"]
        analysis_note = cached_review["analysis_note"]
        external_failure = cached_review.get("external_failure", False) or "Groq synthesis did not complete" in analysis_note
        review_summary = cached_review.get("review_summary", "")
        outcome = cached_review.get("outcome")
    else:
        resume = cached_review or {}
        feedback = list(resume.get("feedback", []))
        similar_count = int(resume.get("similar_count", 0))
        synthesis_calls = int(resume.get("synthesis_calls", 0))
        completed_keys = set(resume.get("completed_group_keys", []))
        legacy_completed_groups = int(resume.get("completed_groups", 0)) if not completed_keys and not resume.get("checkpoint_version") else 0
        analysis_note = resume.get("analysis_note", "")
        partial = bool(resume.get("partial", False))
        external_failure = False
        if "synthesis_plan" in resume:
            groups_to_review = resume["synthesis_plan"]
            hunks_scanned = int(resume.get("hunks_scanned", 0))
            review_summary = resume.get("review_summary", "")
            contexts = resume.get("changed_contexts", {})
            truncated = bool(resume.get("truncated", False))
            logger.info("synthesis_checkpoint_resumed", pr_number=pr_number,
                        completed_groups=len(completed_keys), total_groups=len(groups_to_review))
        else:
            if before_sha and after_sha and after_sha == current_sha:
                diff = await fetch_compare_diff(owner, repo_name, before_sha, after_sha)
            else:
                diff = await fetch_pr_diff(owner, repo_name, pr_number)
            all_hunks = split_diff_into_hunks(diff)
            hunks = all_hunks[:settings.max_pr_hunks]
            truncated = len(all_hunks) > len(hunks)
            hunks_scanned = len(hunks)
            contexts = changed_context_by_file(hunks)
            review_summary = format_change_overview(summarize_diff(diff))
            groups_to_review = []
            for offset in range(0, len(hunks), BATCH_SIZE):
                groups, _ = await retrieve_for_hunks(hunks[offset:offset + BATCH_SIZE], owner, repo_name)
                for group in groups:
                    group["_member_indices"] = [len(groups_to_review)]
                groups_to_review.extend(groups)
            groups_to_review = cluster_repeated_candidate_groups(groups_to_review)
            all_evidence = {}
            for group in groups_to_review:
                for match in group["matches"]:
                    key = match.get("html_url") or (match.get("repo_owner"), match.get("repo_name"), match.get("path"), match.get("body"))
                    all_evidence[key] = match
                group["_review_key"] = hashlib.sha256(f"{group['filepath']}\0{group['hunk']}".encode()).hexdigest()
            similar_count = len(all_evidence)
            if not legacy_completed_groups:
                groups_to_review.sort(key=lambda group: (
                    -max((historical_candidate_rank(group["hunk"], group["filepath"], match, owner, repo_name)
                          for match in group["matches"]), default=0), group["filepath"], group["hunk"],
                ))
            for group in groups_to_review:
                group["changed_context"] = "\n\n".join(
                    f"--- Changed file: {path} ---\n{contexts.get(path, '')}"
                    for path in group.get("filepaths", [group["filepath"]])
                )
            checkpoint_plan = [
                {key: value for key, value in group.items() if key not in {"_symbols", "_evidence"}}
                for group in groups_to_review
            ]
            if save_checkpoint is not None:
                await save_checkpoint({
                    "head_sha": current_sha, "feedback": feedback, "similar_count": similar_count,
                    "hunks_scanned": hunks_scanned, "synthesis_calls": synthesis_calls,
                    "completed_groups": len(completed_keys), "completed_group_keys": sorted(completed_keys),
                    "checkpoint_version": 3, "review_summary": review_summary, "partial": partial,
                    "analysis_note": analysis_note, "complete": False,
                    "synthesis_plan": checkpoint_plan, "changed_contexts": contexts,
                    "truncated": truncated,
                    "deferred_started_at": resume.get("deferred_started_at"),
                    "deferred_attempts": resume.get("deferred_attempts", 0),
                })
        checkpoint_plan = [
            {key: value for key, value in group.items() if key not in {"_symbols", "_evidence"}}
            for group in groups_to_review
        ]
        partial = partial or truncated

        completed_keys = set(completed_keys)
        for group_index, group in enumerate(groups_to_review):
            group_key = group["_review_key"]
            all_legacy_members_completed = (
                legacy_completed_groups > 0
                and all(index < legacy_completed_groups for index in group.get("_member_indices", []))
            )
            if all_legacy_members_completed or group_key in completed_keys:
                continue
            if synthesis_calls >= settings.groq_max_calls_per_pr:
                partial = True
                analysis_note = (
                    "The configured per-PR Groq request limit was reached; "
                    "remaining retrieved hunks were not synthesized."
                )
                break
            synthesis_calls += 1
            try:
                synthesized = await synthesize_feedback(
                    group["hunk"], group["matches"],
                    changed_context=group.get("changed_context", ""),
                    request_context={"pr_number": pr_number, "repo": repo_name,
                                     "synthesis_group_id": group_key},
                )
                synthesized, rejected = remove_context_contradicted_findings(synthesized, contexts)
                if rejected:
                    logger.info(
                        "synthesis_claim_contradicted_by_changed_context",
                        pr_number=pr_number,
                        rejected_count=len(rejected),
                    )
                feedback = deduplicate_findings(feedback + synthesized)
            except GroqRateLimitedError as exc:
                deferred_attempts = int(resume.get("deferred_attempts", 0))
                started_at = resume.get("deferred_started_at")
                if started_at:
                    try:
                        started = datetime.fromisoformat(started_at)
                    except (TypeError, ValueError):
                        started = datetime.utcnow()
                else:
                    started = datetime.utcnow()
                delay = max(exc.retry_after_seconds, 0.0) + random.uniform(0.0, min(3.0, max(0.25, exc.retry_after_seconds * 0.05)))
                elapsed = (datetime.utcnow() - started).total_seconds()
                can_defer = (
                    deferred_attempts < settings.groq_deferred_max_attempts
                    and elapsed + delay <= settings.groq_deferred_max_elapsed_seconds
                    and (claim_attempt is None or claim_attempt < settings.review_max_attempts)
                )
                if can_defer:
                    next_attempt = deferred_attempts + 1
                    checkpoint = {
                        "head_sha": current_sha, "feedback": deduplicate_findings(feedback),
                        "similar_count": similar_count, "hunks_scanned": hunks_scanned,
                        "synthesis_calls": synthesis_calls - 1,
                        "completed_groups": len(completed_keys), "completed_group_keys": sorted(completed_keys),
                        "checkpoint_version": 3, "review_summary": review_summary,
                        "partial": partial, "analysis_note": "", "complete": False,
                        "synthesis_plan": checkpoint_plan, "changed_contexts": contexts,
                        "truncated": truncated, "deferred_started_at": started.isoformat(),
                        "deferred_attempts": next_attempt,
                    }
                    if save_checkpoint is not None:
                        await save_checkpoint(checkpoint)
                    logger.warning("groq_synthesis_deferred", pr_number=pr_number, repo=repo_name,
                                   synthesis_group_id=group_key, model=DEFAULT_MODEL,
                                   retry_after_seconds=exc.retry_after_seconds,
                                   scheduled_delay_seconds=round(delay, 2), retry_count=next_attempt,
                                   cumulative_retry_seconds=round(elapsed + delay, 2))
                    return {"status": "deferred", "pr_number": pr_number,
                            "countdown": max(1, int(delay + 0.999)), "retry_count": next_attempt}
                partial = True
                external_failure = True
                analysis_note = "Groq rate-limit recovery budget was exhausted. This review is incomplete; no no-findings conclusion was reached."
                logger.error("groq_deferred_recovery_exhausted", pr_number=pr_number,
                             repo=repo_name, synthesis_group_id=group_key,
                             retry_after_seconds=exc.retry_after_seconds,
                             retry_count=deferred_attempts, cumulative_retry_seconds=round(elapsed, 2))
                break
            except (GroqSynthesisUnavailable, GroqPermanentError):
                partial = True
                external_failure = True
                analysis_note = (
                    "Groq synthesis failed after its bounded retries. "
                    "This review is incomplete; no no-findings conclusion was reached."
                )
                logger.warning(
                    "pr_synthesis_stopped_after_provider_failure",
                    pr_number=pr_number,
                    repo=repo_name,
                    synthesis_group_id=group_key,
                    model=DEFAULT_MODEL,
                    synthesis_calls=synthesis_calls,
                )
                break

            completed_keys.add(group_key)
            if save_checkpoint is not None:
                await save_checkpoint({
                    "head_sha": current_sha,
                    "feedback": feedback,
                    "similar_count": similar_count,
                    "hunks_scanned": hunks_scanned,
                    "synthesis_calls": synthesis_calls,
                    "completed_groups": len(completed_keys),
                    "completed_group_keys": sorted(completed_keys),
                    "checkpoint_version": 3,
                    "review_summary": review_summary,
                    "partial": partial,
                    "analysis_note": analysis_note,
                    "complete": False,
                    "synthesis_plan": checkpoint_plan,
                    "changed_contexts": contexts,
                    "truncated": truncated,
                    "deferred_started_at": resume.get("deferred_started_at"),
                    "deferred_attempts": resume.get("deferred_attempts", 0),
                })

        feedback = deduplicate_findings(feedback)
        feedback, contradicted = remove_context_contradicted_findings(feedback, contexts)
        if contradicted:
            logger.info(
                "resumed_claim_contradicted_by_changed_context",
                pr_number=pr_number,
                rejected_count=len(contradicted),
            )

        if truncated and not analysis_note:
            analysis_note = (
                f"The diff exceeded the configured limit of {settings.max_pr_hunks} hunks; "
                "remaining hunks were not scanned."
            )
        if external_failure:
            outcome = "external_failure"
        elif partial:
            outcome = "partial_request_limit"
        elif feedback:
            outcome = "completed_with_findings"
        elif similar_count:
            outcome = "completed_no_actionable_findings"
        else:
            outcome = "general_analysis_only"

        checkpoint = {
            "head_sha": current_sha,
            "feedback": feedback,
            "similar_count": similar_count,
            "hunks_scanned": hunks_scanned,
            "synthesis_calls": synthesis_calls,
            "partial": partial,
            "analysis_note": analysis_note,
            "completed_groups": len(completed_keys),
            "completed_group_keys": sorted(completed_keys),
            "checkpoint_version": 3,
            "review_summary": review_summary,
            "external_failure": external_failure,
            "outcome": outcome,
            "complete": (
                not external_failure
                and not partial
                and set(completed_keys).issuperset(group.get("_review_key") for group in groups_to_review)
            ),
            "synthesis_complete": (
                not external_failure
                and set(completed_keys).issuperset(group.get("_review_key") for group in groups_to_review)
            ),
            "synthesis_plan": checkpoint_plan,
            "changed_contexts": contexts,
            "truncated": truncated,
            "deferred_started_at": resume.get("deferred_started_at"),
            "deferred_attempts": resume.get("deferred_attempts", 0),
        }
        if save_checkpoint is not None:
            await save_checkpoint(checkpoint)

    latest_pr_state = await fetch_pr_state(owner, repo_name, pr_number)
    latest_sha = latest_pr_state["head_sha"]
    if latest_pr_state["state"] != "open" or latest_sha != current_sha:
        if latest_pr_state["state"] != "open":
            await _release_closed_pr_claim(owner, repo_name, pr_number, current_sha)
        logger.info(
            "pr_head_changed_during_review_skipped",
            pr_number=pr_number,
            reviewed_head_sha=current_sha,
            current_head_sha=latest_sha,
        )
        return {"status": "obsolete", "pr_number": pr_number}

    if assert_claim is not None:
        await assert_claim()

    if cached_review is not None and cached_review.get("complete", True):
        if not outcome:
            if external_failure:
                outcome = "external_failure"
            elif partial:
                outcome = "partial_request_limit"
            elif feedback:
                outcome = "completed_with_findings"
            elif similar_count:
                outcome = "completed_no_actionable_findings"
            else:
                outcome = "general_analysis_only"

    posted = await post_pr_comment(
        owner,
        repo_name,
        pr_number,
        feedback,
        similar_count=similar_count,
        hunks_scanned=hunks_scanned,
        partial=partial,
        analysis_note=analysis_note,
        head_sha=current_sha,
        outcome=outcome,
        review_summary=review_summary,
    )
    if not posted:
        logger.error("pr_review_github_delivery_failed", pr_number=pr_number, repo=repo_name, head_sha=current_sha)
        return {"status": "github_delivery_failed", "pr_number": pr_number, "head_sha": current_sha}
    logger.info(
        "pr_review_finished",
        owner=owner,
        repo=repo_name,
        pr_number=pr_number,
        head_sha=current_sha,
        hunks_scanned=hunks_scanned,
        synthesis_calls=synthesis_calls,
        findings=len(feedback),
        partial=partial,
        outcome=outcome,
        comment_posted=True,
    )
    return {
        "status": "external_failure" if external_failure else "partial" if partial else "complete",
        "outcome": outcome,
        "analysis_note": analysis_note,
        "pr_number": pr_number,
        "head_sha": current_sha,
        "hunks_scanned": hunks_scanned,
        "synthesis_calls": synthesis_calls,
        "concerns_identified": len(feedback),
        "comment_posted": True,
    }


@celery_app.task(
    name="process_pr", bind=True,
    max_retries=max(settings.review_task_max_retries, settings.groq_deferred_max_attempts),
)
def process_pr(
    self,
    pr_number: int,
    repo_name: str,
    owner: str,
    expected_head_sha: str = None,
    before_sha: str = None,
    after_sha: str = None,
):
    task_id = self.request.id or str(uuid.uuid4())
    delivery_info = self.request.delivery_info or {}
    redelivered = bool(delivery_info.get("redelivered"))
    lease_owner = str(uuid.uuid4())
    if not expected_head_sha:
        try:
            expected_head_sha = asyncio.run(fetch_pr_state(owner, repo_name, pr_number))["head_sha"]
        except Exception as exc:
            logger.error("review_head_lookup_failed", pr_number=pr_number, error_type=type(exc).__name__)
            raise self.retry(exc=exc, countdown=30)

    claim = asyncio.run(_acquire_review_claim(
        owner, repo_name, pr_number, expected_head_sha,
        task_id, lease_owner, redelivered,
    ))
    if claim is None:
        logger.info(
            "review_claim_not_acquired",
            pr_number=pr_number,
            repo=repo_name,
            task_id=task_id,
            redelivered=redelivered,
        )
        return {"status": "already_claimed", "pr_number": pr_number}

    attempt = claim["attempts"]
    logger.info(
        "process_pr_started",
        pr_number=pr_number,
        repo=repo_name,
        attempt=attempt,
        expected_head_sha=expected_head_sha,
        delta_only=bool(before_sha and after_sha),
        recovery_attempt=attempt,
        redelivered=redelivered,
    )

    async def save_checkpoint(payload):
        await _save_review_checkpoint(
            owner, repo_name, pr_number, expected_head_sha, lease_owner, payload
        )

    async def assert_claim():
        await _assert_review_claim(
            owner, repo_name, pr_number, expected_head_sha, lease_owner
        )

    try:
        result = asyncio.run(_process_pr_async(
            pr_number,
            repo_name,
            owner,
            expected_head_sha=expected_head_sha,
            before_sha=before_sha,
            after_sha=after_sha,
            cached_review=claim["review_payload"],
            save_checkpoint=save_checkpoint,
            assert_claim=assert_claim,
            claim_attempt=attempt,
        ))
        if result["status"] == "github_delivery_failed":
            raise RuntimeError("GitHub comment delivery failed after bounded retries")
        if result["status"] == "deferred":
            if attempt >= settings.review_max_attempts:
                raise RuntimeError("review claim recovery budget exhausted before Groq retry")
            asyncio.run(_release_claim_for_retry(
                owner, repo_name, pr_number, expected_head_sha, lease_owner
            ))
            logger.info("pr_task_deferred", pr_number=pr_number, repo=repo_name,
                        retry_count=result["retry_count"], countdown=result["countdown"])
            raise self.retry(
                countdown=result["countdown"],
                max_retries=settings.groq_deferred_max_attempts,
                expires=settings.review_task_expires_seconds,
            )
        if result["status"] == "complete" or result["status"] == "partial":
            durable_status = {
                "completed_with_findings": "completed_with_findings",
                "completed_no_actionable_findings": "completed_no_findings",
                "general_analysis_only": "general_analysis_only",
                "partial_request_limit": "partial_request_limit",
            }.get(result["outcome"], "completed_with_findings")
            finished = asyncio.run(_finish_review_claim(
                owner, repo_name, pr_number, expected_head_sha,
                lease_owner, durable_status,
            ))
            if not finished:
                raise RuntimeError("review claim was lost after GitHub comment delivery")
        elif result["status"] == "external_failure":
            asyncio.run(_record_claim_failure(
                owner, repo_name, pr_number, expected_head_sha, lease_owner,
                result.get("analysis_note", "external service failure"), attempt,
            ))
            logger.error(
                "review_visible_but_marked_failed",
                repo=repo_name,
                pr_number=pr_number,
                outcome=result.get("outcome"),
            )
        elif result["status"] == "closed":
            asyncio.run(_finish_review_claim(
                owner, repo_name, pr_number, expected_head_sha,
                lease_owner, "skipped_closed",
            ))
        elif result["status"] == "obsolete":
            asyncio.run(_finish_review_claim(
                owner, repo_name, pr_number, expected_head_sha,
                lease_owner, "superseded",
            ))
        return result
    except Retry:
        raise
    except SoftTimeLimitExceeded as exc:
        logger.error("process_pr_soft_time_limit_reached", pr_number=pr_number, attempt=attempt)
        error = "soft time limit exceeded"
    except Exception as exc:
        logger.error("process_pr_failed", pr_number=pr_number, error=str(exc), attempt=attempt)
        error = str(exc)

    if self.request.retries < self.max_retries and attempt < settings.review_max_attempts:
        asyncio.run(_release_claim_for_retry(
            owner, repo_name, pr_number, expected_head_sha, lease_owner
        ))
        countdown = min(30 * (2 ** self.request.retries), 180)
        raise self.retry(exc=RuntimeError(error), countdown=countdown)

    asyncio.run(_record_claim_failure(
        owner, repo_name, pr_number, expected_head_sha, lease_owner, error, attempt
    ))
    logger.error("process_pr_dead_lettered", pr_number=pr_number, repo=repo_name, attempts=attempt)
    raise RuntimeError(error)


async def _reconcile_review_jobs_async() -> int:
    now = datetime.utcnow()
    expiry_cutoff = now - timedelta(seconds=settings.review_task_expires_seconds + 60)
    queued = []
    async with AsyncSessionLocal() as session:
        expired = (await session.execute(
            select(ProcessedPR).where(
                ProcessedPR.status == "processing",
                ProcessedPR.lease_expires_at <= now,
            ).limit(100).with_for_update(skip_locked=True)
        )).scalars().all()
        for claim in expired:
            if claim.attempts >= settings.review_max_attempts:
                claim.status = "failed"
                claim.last_error = "review claim lease expired after maximum recovery attempts"
                claim.task_id = None
                claim.lease_owner = None
                claim.lease_expires_at = None
                session.add(FailedPR(
                    repo_owner=claim.repo_owner,
                    repo_name=claim.repo_name,
                    pr_number=claim.pr_number,
                    error=claim.last_error,
                    attempts=claim.attempts,
                ))
                logger.error(
                    "review_recovery_exhausted",
                    repo=claim.repo_name,
                    pr_number=claim.pr_number,
                    attempts=claim.attempts,
                )
            else:
                recovery_delay = min(30 * (2 ** max(0, claim.attempts - 1)), 1800)
                recovery_delay = random.uniform(recovery_delay * 0.8, recovery_delay * 1.2)
                claim.status = "abandoned"
                claim.task_id = None
                claim.lease_owner = None
                claim.lease_expires_at = None
                claim.last_error = "worker claim lease expired; review will be recovered"
                claim.dispatched_at = now - timedelta(
                    seconds=settings.review_task_expires_seconds + 60
                ) + timedelta(seconds=recovery_delay)

        await session.flush()
        due = (await session.execute(
            select(ProcessedPR).where(
                ProcessedPR.status.in_(("queued", "expired", "abandoned")),
                or_(ProcessedPR.dispatched_at.is_(None), ProcessedPR.dispatched_at <= expiry_cutoff),
            ).order_by(ProcessedPR.dispatched_at.asc().nullsfirst()).limit(100)
            .with_for_update(skip_locked=True)
        )).scalars().all()
        for claim in due:
            if (
                claim.attempts >= settings.review_max_attempts
                or claim.dispatch_attempts >= settings.review_max_attempts
            ):
                claim.status = "failed"
                claim.last_error = "review expired from queue after maximum recovery attempts"
                session.add(FailedPR(
                    repo_owner=claim.repo_owner,
                    repo_name=claim.repo_name,
                    pr_number=claim.pr_number,
                    error=claim.last_error,
                    attempts=claim.attempts,
                ))
                continue
            had_dispatch = claim.dispatched_at is not None
            claim.dispatched_at = now
            claim.dispatch_attempts += 1
            if claim.status == "queued" and had_dispatch:
                claim.status = "expired"
                claim.last_error = "queued Celery message expired before a worker claimed it"
            queued.append((claim.pr_number, claim.repo_name, claim.repo_owner, claim.head_sha))
        await session.commit()

    for pr_number, repo_name, owner, head_sha in queued:
        try:
            process_pr.apply_async(
                args=(pr_number, repo_name, owner),
                kwargs={"expected_head_sha": head_sha},
                expires=settings.review_task_expires_seconds,
            )
            logger.warning(
                "abandoned_review_requeued",
                owner=owner,
                repo=repo_name,
                pr_number=pr_number,
                head_sha=head_sha,
            )
        except Exception as exc:
            logger.error(
                "review_reconciliation_publish_failed",
                repo=repo_name,
                pr_number=pr_number,
                error_type=type(exc).__name__,
            )
    return len(queued)


def reconcile_review_jobs() -> int:
    """Requeue expired/abandoned DB claims without scanning or clearing Redis."""
    return asyncio.run(_reconcile_review_jobs_async())
