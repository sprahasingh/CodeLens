import asyncio
import structlog
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy import text
from app.core.database import AsyncSessionLocal
from app.services.evaluation import embed_with_retry
import re


logger = structlog.get_logger()

SIMILARITY_THRESHOLD = 0.55
MAX_RESULTS = 5


async def find_similar_comments(
    hunk: str,
    repo_owner: str,
    repo_name: str,
    limit: int = MAX_RESULTS
) -> List[Dict[str, Any]]:
    """Find review comments from past PRs similar to the given code hunk.

    Searches the full corpus (all indexed repos) so new repos with no
    review history can still benefit from cross-repo pattern matching.
    """

    if len(hunk.strip()) < 30:
        logger.info("hunk_too_short_skipped", hunk_length=len(hunk.strip()))
        return []

    query_vector = await embed_with_retry(hunk)
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
                    html_url,
                    1 - (embedding <=> CAST(:query_vector AS vector)) AS similarity
                FROM review_comments
                WHERE
                    1 - (embedding <=> CAST(:query_vector AS vector)) >= :threshold
                ORDER BY embedding <=> CAST(:query_vector AS vector)
                LIMIT :limit
            """),
            {
                "query_vector": query_vector_str,
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
            "html_url": row.html_url,
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

async def find_similar_comments_for_eval(
    hunk: str,
    before_date,
    repo_owners_and_names: List[tuple],
    limit: int = MAX_RESULTS
) -> List[Dict[str, Any]]:
    """Find similar review comments for held-out evaluation, restricted to a
    date cutoff and a pooled set of repos, to prevent data leakage from the
    held-out test set into the retrieval corpus."""

    if len(hunk.strip()) < 30:
        return []

    query_vector = await embed_with_retry(hunk)
    query_vector_str = "[" + ",".join(str(x) for x in query_vector) + "]"

    repo_conditions = " OR ".join(
        [f"(repo_owner = :owner_{i} AND repo_name = :name_{i})" for i in range(len(repo_owners_and_names))]
    )
    repo_params = {}
    for i, (owner, name) in enumerate(repo_owners_and_names):
        repo_params[f"owner_{i}"] = owner
        repo_params[f"name_{i}"] = name

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(f"""
                SELECT
                    path,
                    line,
                    diff_hunk,
                    body,
                    author,
                    1 - (embedding <=> CAST(:query_vector AS vector)) AS similarity
                FROM review_comments
                WHERE
                    ({repo_conditions})
                    AND comment_created_at < :before_date
                    AND 1 - (embedding <=> CAST(:query_vector AS vector)) >= :threshold
                ORDER BY embedding <=> CAST(:query_vector AS vector)
                LIMIT :limit
            """),
            {
                "query_vector": query_vector_str,
                "before_date": before_date,
                "threshold": SIMILARITY_THRESHOLD,
                "limit": limit,
                **repo_params
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

    return deduplicate_by_body(results)

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
) -> Tuple[List[Dict[str, Any]], int]:
    """Retrieve similar past review comments for each hunk in a PR diff.

    Returns (grouped, total_hunks) where grouped is a list of
    {"hunk": ..., "filepath": ..., "matches": [...]} dicts and total_hunks
    is the count of all hunks extracted (including those with no matches)."""

    hunks = split_diff_into_hunks(diff)
    total_hunks = len(hunks)
    logger.info("pr_hunks_extracted", count=total_hunks)

    grouped = []
    for i, (filepath, hunk) in enumerate(hunks):
        if i > 0:
            await asyncio.sleep(20)
        similar = await find_similar_comments(hunk, repo_owner, repo_name)
        if similar:
            logger.info(
                "hunk_matched",
                hunk_index=i,
                filepath=filepath,
                matches=len(similar),
                top_similarity=similar[0]["similarity"]
            )
            for s in similar:
                s["triggered_by_hunk"] = hunk
                s["triggered_by_file"] = filepath
            grouped.append({"hunk": hunk, "filepath": filepath, "matches": similar})

    logger.info(
        "pr_retrieval_complete",
        total_hunks=total_hunks,
        hunks_with_matches=len(grouped)
    )
    return grouped, total_hunks


def split_diff_into_hunks(diff: str) -> List[Tuple[str, str]]:
    """Split a raw git diff into (filepath, hunk) tuples.

    Tracks the current file from '+++ b/<path>' headers so each hunk carries
    the file it belongs to. Lines before the first '@@' that are not file
    headers are diff metadata and are discarded."""

    hunks: List[Tuple[str, str]] = []
    current_hunk: List[str] = []
    current_file = ""
    for line in diff.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@"):
            if current_hunk:
                hunks.append((current_file, "\n".join(current_hunk)))
            current_hunk = [line]
        elif current_hunk:
            current_hunk.append(line)
    if current_hunk:
        hunks.append((current_file, "\n".join(current_hunk)))
    return [(f, h) for f, h in hunks if len(h.strip()) > 30]


def extract_starting_line(hunk: str) -> Optional[int]:
    """Extract the starting line number in the new file from a diff hunk header."""
    match = re.search(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', hunk)
    if match:
        return int(match.group(1))
    return None