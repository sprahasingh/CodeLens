import structlog
from app.core.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(name="process_pr", bind=True, max_retries=3)
def process_pr(self, pr_number: int, repo_name: str, owner: str):
    logger.info("process_pr_started", pr_number=pr_number, repo=repo_name)
    try:
        logger.info("process_pr_complete", pr_number=pr_number)
        return {
            "status": "complete",
            "pr_number": pr_number,
            "repo": repo_name
        }
    except Exception as exc:
        logger.error("process_pr_failed", pr_number=pr_number, error=str(exc))
        raise self.retry(exc=exc, countdown=60)