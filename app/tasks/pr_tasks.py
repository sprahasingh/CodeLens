import asyncio
import structlog
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import delete

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.failed_pr import FailedPR
from app.models.processed_pr import ProcessedPR
from app.services.github_client import fetch_pr_diff, fetch_compare_diff, fetch_pr_state
from app.services.github_comment import post_pr_comment
from app.services.retrieval import retrieve_for_hunks, split_diff_into_hunks
from app.services.synthesizer import GroqSynthesisUnavailable, synthesize_feedback

logger = structlog.get_logger()

BATCH_SIZE = 10


async def _record_failed_pr(owner: str, repo_name: str, pr_number: int, error: str, attempts: int) -> None:
    async with AsyncSessionLocal() as session:
        session.add(FailedPR(
            repo_owner=owner,
            repo_name=repo_name,
            pr_number=pr_number,
            error=error[:2000],
            attempts=attempts
        ))
        await session.commit()


async def _release_closed_pr_claim(owner: str, repo_name: str, pr_number: int, head_sha: str) -> None:
    """Allow a closed-before-review PR to be queued when it is later reopened."""
    async with AsyncSessionLocal() as session:
        await session.execute(delete(ProcessedPR).where(
            ProcessedPR.repo_owner == owner,
            ProcessedPR.repo_name == repo_name,
            ProcessedPR.pr_number == pr_number,
            ProcessedPR.head_sha == head_sha,
        ))
        await session.commit()


async def _process_pr_async(
    pr_number: int,
    repo_name: str,
    owner: str,
    expected_head_sha: str,
    before_sha: str = None,
    after_sha: str = None,
) -> dict:
    pr_state = await fetch_pr_state(owner, repo_name, pr_number)
    current_sha = pr_state["head_sha"]
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

    if before_sha and after_sha and after_sha == current_sha:
        diff = await fetch_compare_diff(owner, repo_name, before_sha, after_sha)
    else:
        diff = await fetch_pr_diff(owner, repo_name, pr_number)

    all_hunks = split_diff_into_hunks(diff)
    hunks = all_hunks[:settings.max_pr_hunks]
    truncated = len(all_hunks) > len(hunks)
    if not hunks:
        posted = await post_pr_comment(
            owner, repo_name, pr_number, [], hunks_scanned=0, head_sha=current_sha
        )
        if not posted:
            logger.error("pr_review_github_delivery_failed", pr_number=pr_number, repo=repo_name, head_sha=current_sha)
            return {"status": "github_delivery_failed", "pr_number": pr_number, "hunks_scanned": 0}
        return {"status": "complete", "pr_number": pr_number, "hunks_scanned": 0, "comment_posted": True}

    feedback = []
    similar_count = 0
    synthesis_calls = 0
    analysis_note = ""
    partial = truncated

    for offset in range(0, len(hunks), BATCH_SIZE):
        batch = hunks[offset:offset + BATCH_SIZE]
        groups, _ = await retrieve_for_hunks(batch, owner, repo_name)
        for group in groups:
            similar_count += len(group["matches"])
            if synthesis_calls >= settings.groq_max_calls_per_pr:
                partial = True
                analysis_note = (
                    "The configured per-PR Groq request limit was reached; "
                    "remaining retrieved hunks were not synthesized."
                )
                break
            synthesis_calls += 1
            try:
                feedback.extend(await synthesize_feedback(group["hunk"], group["matches"]))
            except GroqSynthesisUnavailable:
                partial = True
                analysis_note = (
                    "Groq synthesis did not complete after the configured bounded retries. "
                    "This result is incomplete and does not mean that no issues exist."
                )
                logger.warning(
                    "pr_synthesis_stopped_after_provider_failure",
                    pr_number=pr_number,
                    synthesis_calls=synthesis_calls,
                )
                break
        if analysis_note:
            break

    if truncated and not analysis_note:
        analysis_note = (
            f"The diff exceeded the configured limit of {settings.max_pr_hunks} hunks; "
            "remaining hunks were not scanned."
        )

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

    posted = await post_pr_comment(
        owner,
        repo_name,
        pr_number,
        feedback,
        similar_count=similar_count,
        hunks_scanned=len(hunks),
        partial=partial,
        analysis_note=analysis_note,
        head_sha=current_sha,
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
        hunks_scanned=len(hunks),
        synthesis_calls=synthesis_calls,
        findings=len(feedback),
        partial=partial,
        comment_posted=True,
    )
    return {
        "status": "partial" if partial else "complete",
        "pr_number": pr_number,
        "head_sha": current_sha,
        "hunks_scanned": len(hunks),
        "synthesis_calls": synthesis_calls,
        "concerns_identified": len(feedback),
        "comment_posted": True,
    }


@celery_app.task(name="process_pr", bind=True, max_retries=settings.review_task_max_retries)
def process_pr(
    self,
    pr_number: int,
    repo_name: str,
    owner: str,
    expected_head_sha: str = None,
    before_sha: str = None,
    after_sha: str = None,
):
    attempt = self.request.retries + 1
    logger.info(
        "process_pr_started",
        pr_number=pr_number,
        repo=repo_name,
        attempt=attempt,
        expected_head_sha=expected_head_sha,
        delta_only=bool(before_sha and after_sha),
    )
    try:
        return asyncio.run(_process_pr_async(
            pr_number,
            repo_name,
            owner,
            expected_head_sha=expected_head_sha,
            before_sha=before_sha,
            after_sha=after_sha,
        ))
    except SoftTimeLimitExceeded as exc:
        logger.error("process_pr_soft_time_limit_reached", pr_number=pr_number, attempt=attempt)
        asyncio.run(_record_failed_pr(owner, repo_name, pr_number, "soft time limit exceeded", attempt))
        return {"status": "failed", "pr_number": pr_number, "reason": "soft_time_limit"}
    except Exception as exc:
        logger.error("process_pr_failed", pr_number=pr_number, error=str(exc), attempt=attempt)
        if self.request.retries >= self.max_retries:
            asyncio.run(_record_failed_pr(owner, repo_name, pr_number, str(exc), attempt))
            logger.error("process_pr_dead_lettered", pr_number=pr_number, repo=repo_name, attempts=attempt)
            raise
        countdown = min(30 * (2 ** self.request.retries), 180)
        raise self.retry(exc=exc, countdown=countdown)
