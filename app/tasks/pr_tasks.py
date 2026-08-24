import asyncio
import structlog
from app.core.celery_app import celery_app
from app.services.github_client import fetch_pr_diff, fetch_pr_review_comments
from app.services.retrieval import find_similar_comments

logger = structlog.get_logger()


def split_diff_into_hunks(diff: str) -> list:
    hunks = []
    current_hunk = []
    for line in diff.split("\n"):
        if line.startswith("@@"):
            if current_hunk:
                hunks.append("\n".join(current_hunk))
            current_hunk = [line]
        else:
            current_hunk.append(line)
    if current_hunk:
        hunks.append("\n".join(current_hunk))
    return [h for h in hunks if len(h.strip()) > 20]


@celery_app.task(name="process_pr", bind=True, max_retries=3)
def process_pr(self, pr_number: int, repo_name: str, owner: str):
    logger.info("process_pr_started", pr_number=pr_number, repo=repo_name)
    try:
        diff = asyncio.run(fetch_pr_diff(owner, repo_name, pr_number))
        hunks = split_diff_into_hunks(diff)

        logger.info("hunks_extracted", pr_number=pr_number, hunk_count=len(hunks))

        all_similar = []
        for hunk in hunks:
            similar = asyncio.run(
                find_similar_comments(hunk, owner, repo_name)
            )
            all_similar.extend(similar)

        logger.info(
            "retrieval_complete",
            pr_number=pr_number,
            hunks_searched=len(hunks),
            similar_comments_found=len(all_similar)
        )

        for comment in all_similar:
            logger.info(
                "retrieved_comment",
                similarity=comment["similarity"],
                body=comment["body"],
                path=comment["path"]
            )

        return {
            "status": "retrieved",
            "pr_number": pr_number,
            "hunks_searched": len(hunks),
            "similar_comments_found": len(all_similar)
        }

    except Exception as exc:
        logger.error("process_pr_failed", pr_number=pr_number, error=str(exc))
        raise self.retry(exc=exc, countdown=60)