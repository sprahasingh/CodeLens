import asyncio
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from app.core.database import AsyncSessionLocal
from app.services.github_client import fetch_all_review_comments
from app.services.embedder import embed_texts
from app.models.review_comment import ReviewComment
from app.services.github_client import fetch_public_review_comments
from sqlalchemy import text

logger = structlog.get_logger()

BATCH_SIZE = 50
PUBLIC_INGEST_BATCH_SIZE = 10


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


async def find_similar_comments(hunk: str, limit: int = 5) -> list:
    from app.services.embedder import embed_single

    query_vector = embed_single(hunk)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, path, line, diff_hunk, body, author,
                       1 - (embedding <=> :query_vector) as similarity
                FROM review_comments
                ORDER BY embedding <=> :query_vector
                LIMIT :limit
            """),
            {
                "query_vector": str(query_vector),
                "limit": limit
            }
        )
        rows = result.fetchall()

    return [
        {
            "path": row.path,
            "line": row.line,
            "diff_hunk": row.diff_hunk,
            "body": row.body,
            "author": row.author,
            "similarity": row.similarity
        }
        for row in rows
    ]


async def ingest_public_repository(owner: str, repo: str, max_comments: int = 2000) -> dict:
    """Ingest review comments from a public repository for corpus backfill."""

    comments = await fetch_public_review_comments(owner, repo, max_comments)

    if not comments:
        logger.warning("no_public_comments_fetched", owner=owner, repo=repo)
        return {"ingested": 0, "skipped": 0}

    comment_ids = [c["id"] for c in comments]
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT github_comment_id FROM review_comments WHERE github_comment_id = ANY(:ids)"),
            {"ids": comment_ids}
        )
        existing_ids = {row[0] for row in result.fetchall()}

    logger.info(
        "idempotency_debug",
        total_fetched_ids=len(comment_ids),
        sample_fetched_ids=comment_ids[:5],
        existing_ids_found=len(existing_ids),
        sample_existing_ids=list(existing_ids)[:5]
    )

    existing_ids_int = {int(x) for x in existing_ids}

    logger.info(
        "pre_filter_debug",
        comments_type=type(comments).__name__,
        comments_len=len(comments),
        first_comment_id=comments[0]["id"] if comments else None,
        first_comment_id_type=type(comments[0]["id"]).__name__ if comments else None,
        existing_sample=list(existing_ids_int)[:3]
    )

    if comments:
        test_id = comments[0]["id"]
        logger.info(
            "membership_test",
            test_id=test_id,
            test_id_type=type(test_id).__name__,
            existing_ids_int_len=len(existing_ids_int),
            is_in_existing=int(test_id) in existing_ids_int
        )

    new_comments = []
    for c in comments:
        cid = int(c["id"])
        if cid not in existing_ids_int:
            new_comments.append(c)
    already_skipped = len(comments) - len(new_comments)

    logger.info(
        "public_ingestion_filtered",
        owner=owner,
        repo=repo,
        total_fetched=len(comments),
        already_in_db=already_skipped,
        new_to_embed=len(new_comments)
    )

    if not new_comments:
        logger.info("public_ingestion_nothing_new", owner=owner, repo=repo)
        return {"ingested": 0, "skipped": already_skipped}

    ingested = 0
    skipped = already_skipped
    num_batches = (len(new_comments) + PUBLIC_INGEST_BATCH_SIZE - 1) // PUBLIC_INGEST_BATCH_SIZE

    for i in range(0, len(new_comments), PUBLIC_INGEST_BATCH_SIZE):
        batch_num = i // PUBLIC_INGEST_BATCH_SIZE + 1

        if batch_num > 1:
            await asyncio.sleep(35)

        batch = new_comments[i:i + PUBLIC_INGEST_BATCH_SIZE]
        hunks = [c["diff_hunk"] for c in batch if c.get("diff_hunk")]

        if not hunks:
            continue

        embeddings = embed_texts(hunks)

        async with AsyncSessionLocal() as session:
            for comment, embedding in zip(batch, embeddings):
                pr_number = 0
                if comment.get("pull_request_url"):
                    pr_number = int(comment["pull_request_url"].split("/")[-1])

                stmt = insert(ReviewComment).values(
                    github_comment_id=comment["id"],
                    repo_owner=owner,
                    repo_name=repo,
                    pr_number=pr_number,
                    path=comment.get("path", ""),
                    line=comment.get("line"),
                    diff_hunk=comment.get("diff_hunk", ""),
                    body=comment.get("body", ""),
                    author=comment.get("user", {}).get("login", "unknown"),
                    embedding=embedding
                ).on_conflict_do_nothing(index_elements=["github_comment_id"])
                result = await session.execute(stmt)
                if result.rowcount > 0:
                    ingested += 1
                else:
                    skipped += 1
            await session.commit()

        logger.info(
            "public_batch_ingested",
            batch=batch_num,
            total_batches=num_batches,
            owner=owner,
            repo=repo,
            ingested=ingested,
            skipped=skipped
        )

    logger.info(
        "public_ingestion_complete",
        owner=owner,
        repo=repo,
        ingested=ingested,
        skipped=skipped
    )
    return {"ingested": ingested, "skipped": skipped}