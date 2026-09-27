import structlog
from typing import List, Dict, Any
from app.services.github_auth import get_github_client
from app.models.prediction import Prediction
from app.core.database import AsyncSessionLocal
from app.services.retrieval import extract_starting_line

logger = structlog.get_logger()

COMMENT_HEADER = "## CodeLens Pre-Review Analysis\n\n"
COMMENT_FOOTER = "\n\n---\n*This analysis was generated automatically by CodeLens based on historical review patterns.*"


def extract_relevant_lines(hunk: str, max_lines: int = 8) -> str:
    lines = hunk.split("\n")
    relevant = []
    in_multiline_string = False
    for line in lines:
        if not line.startswith("+"):
            continue
        if line.startswith("+++"):
            continue
        clean = line[1:].strip()
        if not clean:
            continue
        if clean.startswith("import ") or clean.startswith("from "):
            continue
        if clean.startswith("logger = "):
            continue
        if clean.startswith("client = "):
            continue
        if "= structlog" in clean:
            continue
        if '"""' in clean or "'''" in clean:
            in_multiline_string = not in_multiline_string
            continue
        if in_multiline_string:
            continue
        relevant.append(line[1:])

    if not relevant:
        return ""

    preview = "\n".join(relevant[:max_lines])
    if len(relevant) > max_lines:
        preview += f"\n... ({len(relevant) - max_lines} more lines)"
    return preview


_EXT_TO_LANG = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".rb": "ruby", ".cs": "csharp",
    ".cpp": "cpp", ".c": "c", ".sh": "bash", ".yaml": "yaml",
    ".yml": "yaml", ".json": "json", ".sql": "sql", ".md": "markdown",
}

def _lang_from_file(filepath: str) -> str:
    ext = "." + filepath.rsplit(".", 1)[-1] if "." in filepath else ""
    return _EXT_TO_LANG.get(ext, "")


def format_feedback_as_markdown(
    feedback: List[Dict[str, Any]],
    similar_count: int = 0,
    hunks_scanned: int = 0,
    batch_num: int = 0,
    total_batches: int = 0,
    partial: bool = False
) -> str:
    if not feedback:
        return ""

    lines = [COMMENT_HEADER]

    stats_parts = []
    if total_batches > 1:
        stats_parts.append(f"Batch {batch_num} of {total_batches}")
    if hunks_scanned:
        stats_parts.append(f"{hunks_scanned} hunk{'s' if hunks_scanned != 1 else ''} scanned")
    if similar_count:
        stats_parts.append(f"{similar_count} similar past pattern{'s' if similar_count != 1 else ''} matched")
    if stats_parts:
        lines.append(f"*{' · '.join(stats_parts)}*\n")

    for i, item in enumerate(feedback, 1):
        confidence_pct = int(item.get("confidence", 0) * 100)
        is_inference = item.get("is_inference", False)
        inference_tag = " *(inferred)*" if is_inference else ""
        source_comments = item.get("source_comments", [])

        lines.append("---\n")
        lines.append(f"### Finding {i}: {item.get('concern', '')}{inference_tag}")

        if source_comments:
            sc0 = source_comments[0]
            triggered_file = sc0.get("triggered_by_file", "")
            line_num = extract_starting_line(sc0.get("triggered_by_hunk", ""))
            loc_parts = []
            if triggered_file:
                loc_parts.append(f"`{triggered_file}`")
            if line_num:
                loc_parts.append(f"line {line_num}")
            if loc_parts:
                lines.append(f"**Location:** {' · '.join(loc_parts)}")

        lines.append(f"**Confidence:** {confidence_pct}% | **Suggested check:** {item.get('suggested_check', '')}\n")

        if source_comments:
            triggered_by = source_comments[0].get("triggered_by_hunk", "")
            triggered_file = source_comments[0].get("triggered_by_file", "")
            if triggered_by:
                preview = extract_relevant_lines(triggered_by)
                if preview:
                    lang = _lang_from_file(triggered_file)
                    lines.append("**Your code (this PR):**\n")
                    lines.append(f"```{lang}")
                    lines.append(preview)
                    lines.append("```\n")

        lines.append(f"**Evidence:** {item.get('evidence', '')}\n")

        if source_comments:
            n = len(source_comments)
            lines.append(f"**Past reviews that triggered this ({n} match{'es' if n != 1 else ''}):**")
            lines.append("| Similarity | File | Reviewer comment | Link |")
            lines.append("|---|---|---|---|")
            for sc in source_comments:
                sim = f"{sc['similarity']:.0%}"
                path = f"`{sc['path']}`" if sc.get("path") else "—"
                body = sc.get("body", "").replace("\n", " ").replace("|", "\\|").strip()
                if len(body) > 90:
                    body = body[:90] + "..."
                link = f"[view]({sc['html_url']})" if sc.get("html_url") else "—"
                lines.append(f"| {sim} | {path} | *\"{body}\"* | {link} |")
            lines.append("")

    lines.append(COMMENT_FOOTER)
    if partial:
        lines.append(PARTIAL_NOTICE)
    return "\n".join(lines)

async def save_predictions(
    owner: str,
    repo: str,
    pr_number: int,
    feedback: List[Dict[str, Any]]
) -> None:
    async with AsyncSessionLocal() as session:
        for item in feedback:
            source_comments = item.get("source_comments", [])
            path = source_comments[0]["path"] if source_comments else ""

            predicted_line = None
            if source_comments:
                triggered_by = source_comments[0].get("triggered_by_hunk", "")
                if triggered_by:
                    predicted_line = extract_starting_line(triggered_by)

            prediction = Prediction(
                repo_owner=owner,
                repo_name=repo,
                pr_number=pr_number,
                path=path,
                predicted_line=predicted_line,
                concern=item.get("concern", ""),
                confidence=item.get("confidence", 0.0)
            )
            session.add(prediction)
        await session.commit()

    logger.info(
        "predictions_saved",
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        count=len(feedback)
    )

PARTIAL_NOTICE = (
    "\n\n> **Note:** This PR exceeded the analysis time budget. "
    "The findings above cover the hunks processed before the limit was reached — "
    "remaining hunks were not reviewed."
)


def _no_concerns_body(
    similar_count: int,
    hunks_scanned: int = 0,
    batch_num: int = 0,
    total_batches: int = 0,
    partial: bool = False
) -> str:
    stats_parts = []
    if total_batches > 1:
        stats_parts.append(f"Batch {batch_num} of {total_batches}")
    if hunks_scanned:
        stats_parts.append(f"{hunks_scanned} hunk{'s' if hunks_scanned != 1 else ''} scanned")
    if similar_count > 0:
        stats_parts.append(f"{similar_count} similar past pattern{'s' if similar_count != 1 else ''} found")
    stats_line = f"*{' · '.join(stats_parts)}*\n\n" if stats_parts else ""

    if similar_count > 0:
        conclusion = "No actionable concerns found — similar patterns were retrieved but none applied to the current changes."
    else:
        conclusion = "No concerns found — no similar past patterns matched the changes above the confidence threshold."

    body = (
        "## CodeLens Pre-Review Analysis\n\n"
        + stats_line
        + conclusion + "\n\n"
        "---\n"
        "*This analysis was generated automatically by CodeLens based on historical review patterns.*"
    )
    if partial:
        body += PARTIAL_NOTICE
    return body


async def post_pr_comment(
    owner: str,
    repo: str,
    pr_number: int,
    feedback: List[Dict[str, Any]],
    similar_count: int = 0,
    hunks_scanned: int = 0,
    batch_num: int = 0,
    total_batches: int = 0,
    partial: bool = False
) -> bool:
    body = (
        format_feedback_as_markdown(feedback, similar_count, hunks_scanned, batch_num, total_batches, partial)
        if feedback
        else _no_concerns_body(similar_count, hunks_scanned, batch_num, total_batches, partial)
    )

    try:
        async with await get_github_client() as client:
            response = await client.post(
                f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
                json={"body": body}
            )
            response.raise_for_status()
            comment_url = response.json().get("html_url", "")
            logger.info(
                "pr_comment_posted",
                pr_number=pr_number,
                owner=owner,
                repo=repo,
                comment_url=comment_url
            )
            await save_predictions(owner, repo, pr_number, feedback)
            return True

    except Exception as e:
        logger.error(
            "pr_comment_failed",
            pr_number=pr_number,
            error=str(e)
        )
        return False