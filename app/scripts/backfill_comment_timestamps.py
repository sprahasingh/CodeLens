import asyncio
import structlog
from datetime import datetime
from sqlalchemy import text
from app.core.database import AsyncSessionLocal
from app.services.github_client import fetch_all_review_comments, fetch_public_review_comments

logger = structlog.get_logger()


async def backfill_repo(owner: str, repo: str, is_public: bool) -> dict:
    """Re-fetch comments from GitHub and update comment_created_at for existing rows."""

    if is_public:
        comments = await fetch_public_review_comments(owner, repo, max_comments=2000)
    else:
        comments = await fetch_all_review_comments(owner, repo)

    if not comments:
        logger.warning("backfill_no_comments_fetched", owner=owner, repo=repo)
        return {"updated": 0}

    updated = 0

    async with AsyncSessionLocal() as session:
        for comment in comments:
            if not comment.get("created_at"):
                continue

            comment_created_at = datetime.fromisoformat(
                comment["created_at"].replace("Z", "+00:00")
            ).replace(tzinfo=None)

            result = await session.execute(
                text("""
                    UPDATE review_comments
                    SET comment_created_at = :created_at
                    WHERE github_comment_id = :comment_id
                    AND comment_created_at IS NULL
                """),
                {
                    "created_at": comment_created_at,
                    "comment_id": comment["id"]
                }
            )
            if result.rowcount > 0:
                updated += 1

        await session.commit()

    logger.info(
        "backfill_complete",
        owner=owner,
        repo=repo,
        total_fetched=len(comments),
        updated=updated
    )
    return {"updated": updated}


async def main():
    results = {}

    results["psf/requests"] = await backfill_repo("psf", "requests", is_public=True)
    await asyncio.sleep(65)

    results["tiangolo/fastapi"] = await backfill_repo("tiangolo", "fastapi", is_public=True)
    await asyncio.sleep(65)

    results["sprahasingh/CodeLens"] = await backfill_repo("sprahasingh", "CodeLens", is_public=False)

    logger.info("all_backfills_complete", results=results)


if __name__ == "__main__":
    asyncio.run(main())