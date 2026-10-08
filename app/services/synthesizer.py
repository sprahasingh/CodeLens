import structlog
from typing import List, Dict, Any
from app.services.llm_client import call_groq_json

logger = structlog.get_logger()


class GroqSynthesisUnavailable(RuntimeError):
    """Signals that synthesis failed after the client's bounded retries."""

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
        raise GroqSynthesisUnavailable("Groq did not return a synthesis result")

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
