import asyncio
import structlog
from typing import List, Dict, Any
from sqlalchemy import text
from app.core.database import AsyncSessionLocal
from app.services.embedder import embed_single

logger = structlog.get_logger()

SIMILARITY_THRESHOLD = 0.65
MAX_RESULTS = 5
EF_SEARCH = 40


async def find_similar_comments(
    hunk: str,
    repo_owner: str,
    repo_name: str,
    limit: int = MAX_RESULTS
) -> List[Dict[str, Any]]:
    """Find review comments from past PRs similar to the given code hunk."""

    if len(hunk.strip()) < 30:
        logger.info("hunk_too_short_skipped", hunk_length=len(hunk.strip()))
        return []

    query_vector = embed_single(hunk)
    query_vector_str = "[" + ",".join(str(x) for x in query_vector) + "]"

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT
                    path,
                    line,
                    diff_hunk,
                    body,
                    author,
                    1 - (embedding <=> CAST(:query_vector AS vector)) AS similarity
                FROM review_comments
                WHERE
                    repo_owner = :repo_owner
                    AND repo_name = :repo_name
                    AND 1 - (embedding <=> CAST(:query_vector AS vector)) >= :threshold
                ORDER BY embedding <=> CAST(:query_vector AS vector)
                LIMIT :limit
            """),
            {
                "query_vector": query_vector_str,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "threshold": SIMILARITY_THRESHOLD,
                "limit": limit
            }
        )
        rows = result.fetchall()

    results = [
        {
            "path": row.path,
            "line": row.line,
            "diff_hunk": row.diff_hunk,
            "body": row.body,
            "author": row.author,
            "similarity": round(row.similarity, 3)
        }
        for row in rows
    ]

    deduplicated = deduplicate_by_body(results)

    logger.info(
        "similar_comments_found",
        hunk_preview=hunk[:50],
        repo=f"{repo_owner}/{repo_name}",
        raw_count=len(results),
        deduped_count=len(deduplicated)
    )

    return deduplicated


def deduplicate_by_body(
    results: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    seen_bodies = set()
    deduped = []
    for result in results:
        normalized = result["body"].lower().strip()
        if normalized not in seen_bodies:
            seen_bodies.add(normalized)
            deduped.append(result)
    return deduped


async def retrieve_for_pr(
    diff: str,
    repo_owner: str,
    repo_name: str
) -> List[Dict[str, Any]]:
    hunks = split_diff_into_hunks(diff)
    logger.info("pr_hunks_extracted", count=len(hunks))

    all_results = []
    for i, hunk in enumerate(hunks):
        if i > 0:
            await asyncio.sleep(20)
        similar = await find_similar_comments(hunk, repo_owner, repo_name)
        if similar:
            logger.info(
                "hunk_matched",
                hunk_index=i,
                matches=len(similar),
                top_similarity=similar[0]["similarity"]
            )
            all_results.extend(similar)

    deduplicated = deduplicate_by_body(all_results)
    logger.info(
        "pr_retrieval_complete",
        total_hunks=len(hunks),
        total_matches=len(deduplicated)
    )
    return deduplicated


def split_diff_into_hunks(diff: str) -> List[str]:
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
    return [h for h in hunks if len(h.strip()) > 30]