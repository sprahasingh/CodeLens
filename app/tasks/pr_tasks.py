import asyncio
import structlog
from app.core.celery_app import celery_app
from app.services.github_client import fetch_pr_diff, fetch_pr_review_comments

logger = structlog.get_logger()


@celery_app.task(name="process_pr", bind=True, max_retries=3)
def process_pr(self, pr_number: int, repo_name: str, owner: str):
    logger.info("process_pr_started", pr_number=pr_number, repo=repo_name)
    try:
        diff = asyncio.run(fetch_pr_diff(owner, repo_name, pr_number))
        comments = asyncio.run(fetch_pr_review_comments(owner, repo_name, pr_number))
        logger.info(
            "process_pr_data_fetched",
            pr_number=pr_number,
            diff_length=len(diff),
            existing_comments=len(comments)
        )
        return {
            "status": "fetched",
            "pr_number": pr_number,
            "diff_length": len(diff),
            "existing_comments": len(comments)
        }
    except Exception as exc:
        logger.error("process_pr_failed", pr_number=pr_number, error=str(exc))
        raise self.retry(exc=exc, countdown=60)