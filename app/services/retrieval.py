import asyncio
import structlog
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy import text
from app.core.database import AsyncSessionLocal
from app.services.evaluation import embed_with_retry
from app.services.embedder import embed_texts
import re
from voyageai.error import RateLimitError


logger = structlog.get_logger()

SIMILARITY_THRESHOLD = 0.55
MAX_RESULTS = 5
MAX_REVIEW_WINDOWS_PER_PR = 10
MAX_CANDIDATE_HUNKS = 3
MAX_WINDOW_LINES = 80
WINDOW_OVERLAP_LINES = 6
MAX_WINDOW_CHARS = 4000
MAX_DIFF_LINE_CHARS = 1000
LOW_SIGNAL_FILENAMES = {
    "cargo.lock",
    "composer.lock",
    "gemfile.lock",
    "go.sum",
    "package-lock.json",
    "poetry.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "yarn.lock",
}
HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


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
    return await _find_similar_comments_by_vector(
        query_vector, repo_owner, repo_name, limit, hunk
    )


async def _find_similar_comments_by_vector(
    query_vector: List[float],
    repo_owner: str,
    repo_name: str,
    limit: int = MAX_RESULTS,
    hunk: str = ""
) -> List[Dict[str, Any]]:
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

    Returns matched review windows and the total number of reviewable windows
    before representative sampling, including windows with no matches."""

    all_hunks = split_diff_into_hunks(diff)
    selected_hunks, total_windows = prepare_review_hunks(all_hunks)
    grouped, _ = await retrieve_for_hunks(selected_hunks, repo_owner, repo_name)
    return grouped, total_windows


async def retrieve_for_hunks(
    hunks: List[Tuple[str, str]],
    repo_owner: str,
    repo_name: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """Retrieve similar past review comments for a pre-split list of (filepath, hunk) tuples.
    Used by process_pr_batch which receives its batch from the fan-out task."""
    eligible_hunks = [
        (filepath, hunk)
        for filepath, hunk in hunks
        if not is_low_signal_path(filepath)
    ]
    skipped_hunks = len(hunks) - len(eligible_hunks)
    if skipped_hunks:
        logger.info("low_signal_hunks_skipped", count=skipped_hunks)
    if not eligible_hunks:
        return [], 0

    embeddings = await _embed_hunks_with_retry([hunk for _, hunk in eligible_hunks])
    grouped = []
    for i, ((filepath, hunk), query_vector) in enumerate(zip(eligible_hunks, embeddings)):
        similar = await _find_similar_comments_by_vector(
            query_vector, repo_owner, repo_name, hunk=hunk
        )
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
    grouped.sort(
        key=lambda group: (
            -group["matches"][0]["similarity"],
            -len(group["matches"])
        )
    )
    selected = grouped[:MAX_CANDIDATE_HUNKS]
    logger.info(
        "batch_retrieval_complete",
        grouped_hunks=len(grouped),
        selected_hunks=len(selected),
        total_hunks=len(eligible_hunks)
    )
    return selected, len(eligible_hunks)


def is_low_signal_path(filepath: str) -> bool:
    filename = filepath.rsplit("/", 1)[-1].lower()
    return filename in LOW_SIGNAL_FILENAMES or filename.endswith((".min.js", ".min.css"))


def prepare_review_hunks(
    hunks: List[Tuple[str, str]],
    limit: int = MAX_REVIEW_WINDOWS_PER_PR
) -> Tuple[List[Tuple[str, str]], int]:
    """Bound hunk size, then sample review windows across files and diff positions."""
    windows_by_file: Dict[str, List[Tuple[int, str]]] = {}
    oversized_hunks = 0
    skipped_low_signal = 0
    window_index = 0

    for filepath, hunk in hunks:
        if is_low_signal_path(filepath):
            skipped_low_signal += 1
            continue
        windows = _split_hunk_into_windows(hunk)
        if len(windows) > 1:
            oversized_hunks += 1
        file_windows = windows_by_file.setdefault(filepath, [])
        for window in windows:
            file_windows.append((window_index, window))
            window_index += 1

    total_windows = window_index
    if total_windows <= limit:
        selected = [
            (filepath, hunk)
            for filepath, windows in windows_by_file.items()
            for _, hunk in windows
        ]
    else:
        files = list(windows_by_file)
        allocations = {filepath: 1 for filepath in files}
        if len(files) > limit:
            selected_file_indices = _evenly_spaced_indices(len(files), limit)
            allocations = {files[index]: 1 for index in selected_file_indices}
        else:
            remaining = limit - len(files)
            while remaining:
                eligible_files = [
                    filepath for filepath in files
                    if allocations[filepath] < len(windows_by_file[filepath])
                ]
                if not eligible_files:
                    break
                filepath = max(
                    eligible_files,
                    key=lambda path: len(windows_by_file[path]) / allocations[path]
                )
                allocations[filepath] += 1
                remaining -= 1

        selected_with_indices = []
        for filepath, allocation in allocations.items():
            windows = windows_by_file[filepath]
            for index in _evenly_spaced_indices(len(windows), allocation):
                original_index, hunk = windows[index]
                selected_with_indices.append((original_index, filepath, hunk))
        selected_with_indices.sort(key=lambda item: item[0])
        selected = [(filepath, hunk) for _, filepath, hunk in selected_with_indices]

    logger.info(
        "pr_review_windows_prepared",
        original_hunks=len(hunks),
        reviewable_windows=total_windows,
        selected_windows=len(selected),
        oversized_hunks_split=oversized_hunks,
        low_signal_hunks_skipped=skipped_low_signal
    )
    return selected, total_windows


def _evenly_spaced_indices(length: int, count: int) -> List[int]:
    if count <= 0 or length <= 0:
        return []
    if count == 1:
        return [length // 2]
    return [round(index * (length - 1) / (count - 1)) for index in range(count)]


def _split_hunk_into_windows(hunk: str) -> List[str]:
    lines = hunk.splitlines()
    if not lines:
        return []
    match = HUNK_HEADER_RE.match(lines[0])
    if not match:
        logger.warning("diff_hunk_header_unrecognized")
        return [hunk[:MAX_WINDOW_CHARS]]

    old_start = int(match.group(1))
    new_start = int(match.group(3))
    section = match.group(5)[:200]
    body = lines[1:]
    bounded_body = [_bound_diff_line(line) for line in body]
    if (
        len(body) <= MAX_WINDOW_LINES
        and len(lines[0]) + sum(len(line) + 1 for line in bounded_body) <= MAX_WINDOW_CHARS
        and bounded_body == body
    ):
        return [hunk]

    offsets = []
    old_offset = 0
    new_offset = 0
    for line in body:
        offsets.append((old_offset, new_offset))
        if line.startswith((" ", "-")):
            old_offset += 1
        if line.startswith((" ", "+")):
            new_offset += 1

    windows = []
    start = 0
    while start < len(body):
        end = start
        chars = len(lines[0])
        while end < len(body) and end - start < MAX_WINDOW_LINES:
            line = bounded_body[end]
            if end > start and chars + len(line) + 1 > MAX_WINDOW_CHARS:
                break
            chars += len(line) + 1
            end += 1
        if end == start:
            end += 1

        chunk = bounded_body[start:end]
        if any(line.startswith(("+", "-")) for line in chunk):
            old_count = sum(line.startswith((" ", "-")) for line in chunk)
            new_count = sum(line.startswith((" ", "+")) for line in chunk)
            chunk_header = (
                f"@@ -{old_start + offsets[start][0]},{old_count} "
                f"+{new_start + offsets[start][1]},{new_count} @@{section}"
            )
            windows.append("\n".join([chunk_header] + chunk))

        if end == len(body):
            break
        start = max(start + 1, end - WINDOW_OVERLAP_LINES)

    return windows or ["\n".join([lines[0]] + bounded_body[:MAX_WINDOW_LINES])]


def _bound_diff_line(line: str) -> str:
    if len(line) <= MAX_DIFF_LINE_CHARS:
        return line
    prefix = line[:1] if line[:1] in ("+", "-", " ") else ""
    content = line[len(prefix):]
    keep = (MAX_DIFF_LINE_CHARS - len(prefix) - len("...[line clipped]...")) // 2
    return prefix + content[:keep] + "...[line clipped]..." + content[-keep:]


async def _embed_hunks_with_retry(hunks: List[str]) -> List[List[float]]:
    for attempt in range(1, 4):
        try:
            return embed_texts(hunks)
        except RateLimitError:
            if attempt == 3:
                raise
            logger.warning(
                "voyage_rate_limited_retrying_batch",
                attempt=attempt,
                wait_seconds=65
            )
            await asyncio.sleep(65)
    return []


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