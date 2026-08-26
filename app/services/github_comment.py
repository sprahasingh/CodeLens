import structlog
from typing import List, Dict, Any
from app.services.github_auth import get_github_client

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


def format_feedback_as_markdown(
    feedback: List[Dict[str, Any]],
    similar_comments: List[Dict[str, Any]] = []
) -> str:
    if not feedback:
        return ""

    lines = [COMMENT_HEADER]

    for i, item in enumerate(feedback, 1):
        confidence_pct = int(item.get("confidence", 0) * 100)
        is_inference = item.get("is_inference", False)
        inference_tag = " *(inferred)*" if is_inference else ""

        lines.append(f"---")
        lines.append(f"### Finding {i}: {item.get('concern', '')}{inference_tag}")
        lines.append(f"**Confidence:** {confidence_pct}% | **Suggested check:** {item.get('suggested_check', '')}")
        lines.append("")

        source_comments = item.get("source_comments", [])

        if source_comments:
            for sc in source_comments:
                triggered_by = sc.get('triggered_by_hunk', '')
                if triggered_by:
                    current_preview = extract_relevant_lines(triggered_by)
                    if current_preview:
                        lines.append("**Your code (this PR):**")
                        lines.append("")
                        lines.append("```python")
                        lines.append(current_preview)
                        lines.append("```")
                        lines.append("")
                        break

            for sc in source_comments:
                line_ref = f" line {sc['line']}" if sc.get('line') else ""
                past_preview = extract_relevant_lines(sc.get('diff_hunk', ''))
                lines.append(f"**Similar past code** — `{sc['path']}`{line_ref} (similarity: {sc['similarity']:.0%}):")
                lines.append("")
                if past_preview:
                    lines.append("```python")
                    lines.append(past_preview)
                    lines.append("```")
                    lines.append("")
                lines.append(f"**Past reviewer said:** *\"{sc['body']}\"*")
                lines.append("")

        lines.append(f"**Evidence:** {item.get('evidence', '')}")
        lines.append("")

    lines.append(COMMENT_FOOTER)
    return "\n".join(lines)


async def post_pr_comment(
    owner: str,
    repo: str,
    pr_number: int,
    feedback: List[Dict[str, Any]],
    similar_comments: List[Dict[str, Any]] = []
) -> bool:
    if not feedback:
        logger.info("no_feedback_to_post", pr_number=pr_number)
        return False

    body = format_feedback_as_markdown(feedback, similar_comments)

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
            return True

    except Exception as e:
        logger.error(
            "pr_comment_failed",
            pr_number=pr_number,
            error=str(e)
        )
        return False