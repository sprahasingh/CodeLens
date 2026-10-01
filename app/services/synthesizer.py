import structlog
from typing import List, Dict, Any, Optional
from app.services.llm_client import call_groq_json

logger = structlog.get_logger()

MAX_SYNTHESIS_HUNKS = 3
MAX_SOURCE_COMMENTS_PER_HUNK = 5
MAX_PROMPT_HUNK_CHARS = 4000
MAX_PROMPT_COMMENT_CHARS = 600
MAX_PROMPT_PATH_CHARS = 200
MAX_BATCH_PROMPT_CHARS = 28000

SYNTHESIS_PROMPT = """You are a senior software engineer reviewing a pull request.

The new code being reviewed:
```
{hunk}
```

You have been given a list of past review comments that were left on code similar to the new code above. Each comment has an index number.

Past review comments retrieved:
{comments}

For each distinct concern that genuinely applies to the specific new code shown above, add an item to a "concerns" array where each item has exactly these fields:
- concern: brief description of the issue (1 sentence), specific to what is actually in the new code above
- evidence: what in the past reviews suggests this concern (1 sentence)
- confidence: float between 0.0 and 1.0
- suggested_check: specific thing the developer should verify (1 sentence)
- is_inference: true if inferred from patterns, false if directly stated
- source_indices: list of integer indices of the past comments that support this concern

Only include a concern if the new code above actually exhibits the issue — a similar past comment existing is not sufficient on its own. If none of the past comments describe an issue that is actually present in the new code, return an empty "concerns" array.

Respond with ONLY a valid JSON object of the form {{"concerns": [...]}}. No preamble, no explanation, no markdown.
Example:
{{"concerns": [
  {{
    "concern": "Missing error handling for database queries",
    "evidence": "Past reviewers flagged similar database calls that returned None silently",
    "confidence": 0.85,
    "suggested_check": "Verify that None return values are handled explicitly",
    "is_inference": false,
    "source_indices": [0, 2]
  }}
]}}"""

BATCH_SYNTHESIS_PROMPT = """You are a senior software engineer reviewing a pull request.

Candidate hunks from the pull request:
{hunks}

Past review comments retrieved for these candidates:
{comments}

For each candidate hunk, identify at most one distinct concern that genuinely applies to its new code. Similar past comments are evidence, not proof that the new code has the issue. If none applies, return no concern for that hunk.

Respond with ONLY a valid JSON object. Each concern must have exactly these fields:
- hunk_index: integer index of the candidate hunk this concern applies to
- concern: brief description of the issue (1 sentence)
- evidence: what in the past reviews suggests this concern (1 sentence)
- confidence: float between 0.0 and 1.0
- suggested_check: specific thing the developer should verify (1 sentence)
- is_inference: true if inferred from patterns, false if directly stated
- source_indices: list of indices of past comments that support this concern; only use comments retrieved for the selected hunk

Return an empty concerns array when none apply. Example:
{{"concerns": [{{"hunk_index": 0, "concern": "Missing error handling", "evidence": "Similar code was flagged by reviewers", "confidence": 0.8, "suggested_check": "Verify failures are handled", "is_inference": true, "source_indices": [0]}}]}}"""


async def synthesize_feedback(
    hunk: str,
    similar_comments: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not similar_comments:
        logger.info("no_comments_to_synthesize")
        return []

    comments_text = "\n".join([
        f"[{i}] [{c['similarity']:.2f} similarity] {c['body']} (from {c['path']})"
        for i, c in enumerate(similar_comments)
    ])

    prompt = SYNTHESIS_PROMPT.format(hunk=hunk, comments=comments_text)

    result = await call_groq_json(prompt)
    if result is None:
        logger.error("synthesis_failed")
        return []

    raw_feedback = result.get("concerns", [])

    # A concern with no text can't be embedded, matched, judged, or rendered
    # downstream — drop it here so every consumer can assume concern is non-empty.
    feedback = [
        item for item in raw_feedback
        if isinstance(item.get("concern"), str) and item["concern"].strip()
    ]

    for item in feedback:
        indices = item.get("source_indices", [])
        item["source_comments"] = [
            similar_comments[i]
            for i in indices
            if isinstance(i, int) and 0 <= i < len(similar_comments)
        ]

    logger.info(
        "synthesis_complete",
        input_comments=len(similar_comments),
        output_concerns=len(feedback)
    )
    return feedback


async def synthesize_feedback_for_hunks(
    hunk_groups: List[Dict[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    if not hunk_groups:
        return []

    hunk_groups = hunk_groups[:MAX_SYNTHESIS_HUNKS]
    hunk_sections = []
    comments = []
    comment_sections = []
    comment_hunk_indices = []
    for hunk_index, group in enumerate(hunk_groups):
        hunk_sections.append(
            f"[{hunk_index}] File: "
            f"{_prompt_excerpt(group['filepath'], MAX_PROMPT_PATH_CHARS)}\n"
            f"{_prompt_excerpt(group['hunk'], MAX_PROMPT_HUNK_CHARS, preserve_ends=True)}"
        )
        for comment in group["matches"][:MAX_SOURCE_COMMENTS_PER_HUNK]:
            comment_index = len(comments)
            comments.append(comment)
            comment_hunk_indices.append(hunk_index)
            comment_sections.append(
                f"[{comment_index}] [candidate hunk {hunk_index}] "
                f"[{comment['similarity']:.2f} similarity] "
                f"{_prompt_excerpt(comment['body'], MAX_PROMPT_COMMENT_CHARS)} "
                f"(from {_prompt_excerpt(comment['path'], MAX_PROMPT_PATH_CHARS)})"
            )

    prompt = BATCH_SYNTHESIS_PROMPT.format(
        hunks="\n\n".join(hunk_sections),
        comments="\n".join(comment_sections)
    )
    result = await call_groq_json(prompt)
    if result is None:
        logger.warning("batch_synthesis_unavailable")
        return None

    raw_feedback = result.get("concerns", [])
    if not isinstance(raw_feedback, list):
        return []

    feedback = []
    for item in raw_feedback:
        if not isinstance(item, dict):
            continue
        concern = item.get("concern")
        hunk_index = item.get("hunk_index")
        if (
            not isinstance(concern, str)
            or not concern.strip()
            or not isinstance(hunk_index, int)
            or isinstance(hunk_index, bool)
            or not 0 <= hunk_index < len(hunk_groups)
        ):
            continue

        indices = item.get("source_indices", [])
        if not isinstance(indices, list):
            continue
        source_comments = [
            comments[index]
            for index in indices
            if isinstance(index, int)
            and not isinstance(index, bool)
            and 0 <= index < len(comments)
            and comment_hunk_indices[index] == hunk_index
        ]
        if not source_comments:
            continue
        item["source_comments"] = source_comments
        feedback.append(item)

    logger.info(
        "batch_synthesis_complete",
        candidate_hunks=len(hunk_groups),
        input_comments=len(comments),
        output_concerns=len(feedback),
        prompt_chars=len(prompt),
        prompt_char_budget=MAX_BATCH_PROMPT_CHARS
    )
    return feedback


def _prompt_excerpt(value: str, max_chars: int, preserve_ends: bool = False) -> str:
    if len(value) <= max_chars:
        return value
    if preserve_ends:
        marker = "\n...[middle truncated]...\n"
        available = max_chars - len(marker)
        start_chars = available // 2
        return value[:start_chars] + marker + value[-(available - start_chars):]
    marker = "...[truncated]"
    return value[:max_chars - len(marker)] + marker
