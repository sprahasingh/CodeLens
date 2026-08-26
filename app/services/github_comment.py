import structlog
from typing import List, Dict, Any
from app.services.github_auth import get_github_client

logger = structlog.get_logger()

COMMENT_HEADER = "## CodeLens Pre-Review Analysis\n\n"
COMMENT_FOOTER = "\n\n---\n*This analysis was generated automatically by CodeLens based on historical review patterns.*"


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

        lines.append(f"### {i}. {item.get('concern', '')}{inference_tag}")
        lines.append(f"**Confidence:** {confidence_pct}%")
        lines.append(f"**Evidence:** {item.get('evidence', '')}")
        lines.append(f"**Suggested check:** {item.get('suggested_check', '')}")
        lines.append("")

    if similar_comments:
        lines.append("### Retrieved from past reviews")
        for c in similar_comments:
            lines.append(
                f"- `{c['path']}` (similarity: {c['similarity']:.0%}): *\"{c['body']}\"*"
            )
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