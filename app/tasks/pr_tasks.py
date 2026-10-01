import asyncio
import structlog
from app.core.celery_app import celery_app
from app.services.github_client import fetch_pr_diff, fetch_compare_diff
from app.services.retrieval import (
    retrieve_for_hunks,
    split_diff_into_hunks,
    prepare_review_hunks,
)
from app.services.synthesizer import synthesize_feedback_for_hunks
from app.services.github_comment import post_pr_comment
from app.core.database import AsyncSessionLocal
from app.models.failed_pr import FailedPR

logger = structlog.get_logger()

async def _fan_out_async(
    pr_number: int,
    repo_name: str,
    owner: str,
    before_sha: str = None,
    after_sha: str = None
) -> dict:
    if before_sha and after_sha:
        diff = await fetch_compare_diff(owner, repo_name, before_sha, after_sha)
    else:
        diff = await fetch_pr_diff(owner, repo_name, pr_number)
    all_hunks = split_diff_into_hunks(diff)
    total_hunks = len(all_hunks)

    if not all_hunks:
        await post_pr_comment(owner, repo_name, pr_number, [], similar_count=0, hunks_scanned=0)
        logger.info("no_hunks_found", pr_number=pr_number)
        return {"status": "complete", "pr_number": pr_number, "total_batches": 0, "total_hunks": 0}

    selected_hunks, total_windows = prepare_review_hunks(all_hunks)
    if not selected_hunks:
        logger.info("pr_review_skipped_no_reviewable_hunks_posting_status", pr_number=pr_number)
        posted = await post_pr_comment(
            owner, repo_name, pr_number, [],
            review_skipped=True
        )
        return {
            "status": "no_reviewable_hunks",
            "pr_number": pr_number,
            "total_hunks": total_hunks,
            "comment_posted": posted
        }

    if total_windows > len(selected_hunks):
        logger.warning(
            "pr_review_windows_sampled",
            pr_number=pr_number,
            total_windows=total_windows,
            sampled_windows=len(selected_hunks)
        )
    hunks_data = [{"filepath": filepath, "hunk": hunk} for filepath, hunk in selected_hunks]
    process_pr_batch.delay(
        pr_number, repo_name, owner,
        hunks_data, 1, 1, total_windows
    )

    logger.info(
        "pr_batches_queued",
        pr_number=pr_number,
        total_batches=1,
        total_hunks=total_windows,
        hunks_queued=len(hunks_data)
    )
    return {"status": "batched", "pr_number": pr_number, "total_batches": 1, "total_hunks": total_windows}


async def _process_batch_async(
    pr_number: int,
    repo_name: str,
    owner: str,
    hunks_data: list,
    batch_num: int,
    total_batches: int,
    total_hunks: int
) -> dict:
    hunks = [(d["filepath"], d["hunk"]) for d in hunks_data]
    hunk_groups, hunks_scanned = await retrieve_for_hunks(hunks, owner, repo_name)
    batch_matches = sum(len(g["matches"]) for g in hunk_groups)

    logger.info(
        "batch_retrieval_done",
        pr_number=pr_number,
        batch_num=batch_num,
        hunks_with_matches=len(hunk_groups),
        similar_comments_found=batch_matches
    )

    feedback = await synthesize_feedback_for_hunks(hunk_groups)
    if feedback is None:
        logger.warning(
            "pr_analysis_unavailable_posting_status_comment",
            pr_number=pr_number,
            batch_num=batch_num
        )
        hunks_omitted = max(0, total_hunks - hunks_scanned)
        posted = await post_pr_comment(
            owner, repo_name, pr_number, [],
            hunks_scanned=hunks_scanned,
            batch_num=batch_num,
            total_batches=total_batches,
            hunks_omitted=hunks_omitted,
            analysis_unavailable=True
        )
        return {
            "status": "synthesis_unavailable",
            "pr_number": pr_number,
            "batch_num": batch_num,
            "concerns_identified": 0,
            "comment_posted": posted
        }

    logger.info(
        "batch_synthesis_done",
        pr_number=pr_number,
        batch_num=batch_num,
        concerns_identified=len(feedback),
        partial=False
    )

    posted = await post_pr_comment(
        owner, repo_name, pr_number, feedback,
        similar_count=batch_matches,
        hunks_scanned=hunks_scanned,
        batch_num=batch_num,
        total_batches=total_batches,
        partial=False,
        hunks_omitted=max(0, total_hunks - hunks_scanned)
    )

    hunks_omitted = max(0, total_hunks - hunks_scanned)
    return {
        "status": "partial" if hunks_omitted else "complete",
        "pr_number": pr_number,
        "batch_num": batch_num,
        "concerns_identified": len(feedback),
        "comment_posted": posted
    }


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


@celery_app.task(name="process_pr", bind=True, max_retries=3)
def process_pr(self, pr_number: int, repo_name: str, owner: str, before_sha: str = None, after_sha: str = None):
    attempt = self.request.retries + 1
    logger.info(
        "process_pr_started",
        pr_number=pr_number, repo=repo_name, attempt=attempt,
        delta_only=bool(before_sha and after_sha)
    )
    try:
        return asyncio.run(_fan_out_async(pr_number, repo_name, owner, before_sha=before_sha, after_sha=after_sha))
    except Exception as exc:
        logger.error("process_pr_failed", pr_number=pr_number, error=str(exc), attempt=attempt)

        if self.request.retries >= self.max_retries:
            asyncio.run(_record_failed_pr(owner, repo_name, pr_number, str(exc), attempt))
            logger.error("process_pr_dead_lettered", pr_number=pr_number, repo=repo_name, attempts=attempt)
            raise

        raise self.retry(exc=exc, countdown=60)


@celery_app.task(name="process_pr_batch", bind=True, max_retries=3)
def process_pr_batch(
    self,
    pr_number: int,
    repo_name: str,
    owner: str,
    hunks_data: list,
    batch_num: int,
    total_batches: int,
    total_hunks: int
):
    attempt = self.request.retries + 1
    logger.info(
        "process_pr_batch_started",
        pr_number=pr_number,
        batch_num=batch_num,
        total_batches=total_batches,
        hunks_in_batch=len(hunks_data),
        attempt=attempt
    )
    try:
        return asyncio.run(_process_batch_async(
            pr_number, repo_name, owner,
            hunks_data, batch_num, total_batches, total_hunks
        ))
    except Exception as exc:
        logger.error(
            "process_pr_batch_failed",
            pr_number=pr_number,
            batch_num=batch_num,
            error=str(exc)
        )
        raise self.retry(exc=exc, countdown=30)
