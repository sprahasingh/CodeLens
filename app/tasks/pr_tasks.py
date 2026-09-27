import asyncio
import structlog
from app.core.celery_app import celery_app
from app.services.github_client import fetch_pr_diff
from app.services.retrieval import retrieve_for_pr
from app.services.synthesizer import synthesize_feedback
from app.services.github_comment import post_pr_comment
from app.core.database import engine, AsyncSessionLocal
from app.models.failed_pr import FailedPR

logger = structlog.get_logger()


async def _process_pr_async(pr_number: int, repo_name: str, owner: str) -> dict:
    diff = await fetch_pr_diff(owner, repo_name, pr_number)
    hunk_groups, total_hunks = await retrieve_for_pr(diff, owner, repo_name)
    total_matches = sum(len(g["matches"]) for g in hunk_groups)

    logger.info(
        "retrieval_complete",
        pr_number=pr_number,
        hunks_with_matches=len(hunk_groups),
        similar_comments_found=total_matches
    )

    feedback = []
    for i, group in enumerate(hunk_groups):
        if i > 0:
            await asyncio.sleep(10)
        hunk_feedback = await synthesize_feedback(group["hunk"], group["matches"])
        feedback.extend(hunk_feedback)

    logger.info("synthesis_complete", pr_number=pr_number, concerns_identified=len(feedback))

    posted = await post_pr_comment(owner, repo_name, pr_number, feedback, similar_count=total_matches, hunks_scanned=total_hunks)

    logger.info(
        "process_pr_complete",
        pr_number=pr_number,
        similar_comments_found=total_matches,
        concerns_identified=len(feedback),
        comment_posted=posted
    )

    return {
        "status": "complete",
        "pr_number": pr_number,
        "similar_comments_found": total_matches,
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
def process_pr(self, pr_number: int, repo_name: str, owner: str):
    attempt = self.request.retries + 1
    logger.info("process_pr_started", pr_number=pr_number, repo=repo_name, attempt=attempt)
    try:
        return asyncio.run(_process_pr_async(pr_number, repo_name, owner))
    except Exception as exc:
        logger.error("process_pr_failed", pr_number=pr_number, error=str(exc), attempt=attempt)

        if self.request.retries >= self.max_retries:
            asyncio.run(_record_failed_pr(owner, repo_name, pr_number, str(exc), attempt))
            logger.error(
                "process_pr_dead_lettered",
                pr_number=pr_number,
                repo=repo_name,
                attempts=attempt
            )
            raise

        raise self.retry(exc=exc, countdown=60)
