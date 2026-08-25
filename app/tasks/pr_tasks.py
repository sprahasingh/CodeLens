import asyncio
import structlog
from app.core.celery_app import celery_app
from app.services.github_client import fetch_pr_diff, fetch_pr_review_comments
from app.services.retrieval import retrieve_for_pr

logger = structlog.get_logger()


@celery_app.task(name="process_pr", bind=True, max_retries=3)
def process_pr(self, pr_number: int, repo_name: str, owner: str):
    logger.info("process_pr_started", pr_number=pr_number, repo=repo_name)
    try:
        diff = asyncio.run(fetch_pr_diff(owner, repo_name, pr_number))
        similar_comments = asyncio.run(
            retrieve_for_pr(diff, owner, repo_name)
        )

        logger.info(
            "process_pr_retrieval_complete",
            pr_number=pr_number,
            similar_comments_found=len(similar_comments)
        )

        for comment in similar_comments:
            logger.info(
                "retrieved_comment",
                similarity=comment["similarity"],
                body=comment["body"],
                path=comment["path"]
            )

        return {
            "status": "retrieved",
            "pr_number": pr_number,
            "similar_comments_found": len(similar_comments)
        }

    except Exception as exc:
        logger.error("process_pr_failed", pr_number=pr_number, error=str(exc))
        raise self.retry(exc=exc, countdown=60)