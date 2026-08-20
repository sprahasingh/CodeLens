import asyncio
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from app.core.database import AsyncSessionLocal
from app.services.github_client import fetch_all_review_comments
from app.services.embedder import embed_texts
from app.models.review_comment import ReviewComment

logger = structlog.get_logger()

BATCH_SIZE = 50


async def ingest_repository(owner: str, repo: str) -> dict:
    logger.info("ingestion_started", owner=owner, repo=repo)

    comments = await fetch_all_review_comments(owner, repo)
    logger.info("comments_fetched", count=len(comments))

    if not comments:
        logger.info("no_comments_to_ingest", owner=owner, repo=repo)
        return {"ingested": 0, "skipped": 0}

    ingested = 0
    skipped = 0

    for i in range(0, len(comments), BATCH_SIZE):
        batch = comments[i:i + BATCH_SIZE]
        hunks = [c["diff_hunk"] for c in batch]

        embeddings = embed_texts(hunks)

        async with AsyncSessionLocal() as session:
            for comment, embedding in zip(batch, embeddings):
                stmt = insert(ReviewComment).values(
                    github_comment_id=comment["id"],
                    repo_owner=owner,
                    repo_name=repo,
                    pr_number=int(comment["pull_request_url"].split("/")[-1]),
                    path=comment["path"],
                    line=comment.get("line") or comment.get("original_line"),
                    diff_hunk=comment["diff_hunk"],
                    body=comment["body"],
                    author=comment["user"]["login"],
                    embedding=embedding
                ).on_conflict_do_nothing(
                    index_elements=["github_comment_id"]
                )
                result = await session.execute(stmt)
                if result.rowcount > 0:
                    ingested += 1
                else:
                    skipped += 1
            await session.commit()

        logger.info(
            "batch_ingested",
            batch=i // BATCH_SIZE + 1,
            ingested=ingested,
            skipped=skipped
        )

    logger.info(
        "ingestion_complete",
        owner=owner,
        repo=repo,
        ingested=ingested,
        skipped=skipped
    )
    return {"ingested": ingested, "skipped": skipped}