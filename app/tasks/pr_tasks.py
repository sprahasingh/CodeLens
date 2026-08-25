import asyncio
import structlog
from app.core.celery_app import celery_app
from app.services.github_client import fetch_pr_diff
from app.services.retrieval import retrieve_for_pr
from app.services.synthesizer import synthesize_feedback

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
            "retrieval_complete",
            pr_number=pr_number,
            similar_comments_found=len(similar_comments)
        )

        feedback = asyncio.run(synthesize_feedback(similar_comments))

        logger.info(
            "synthesis_complete",
            pr_number=pr_number,
            concerns_identified=len(feedback)
        )

        for item in feedback:
            logger.info(
                "feedback_item",
                concern=item.get("concern"),
                confidence=item.get("confidence"),
                is_inference=item.get("is_inference")
            )

        return {
            "status": "synthesized",
            "pr_number": pr_number,
            "similar_comments_found": len(similar_comments),
            "concerns_identified": len(feedback),
            "feedback": feedback
        }

    except Exception as exc:
        logger.error("process_pr_failed", pr_number=pr_number, error=str(exc))
        raise self.retry(exc=exc, countdown=60)