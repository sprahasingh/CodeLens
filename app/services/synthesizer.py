import structlog
import math
from typing import List, Dict, Any
from app.core.config import settings
from app.services.llm_client import call_groq_json, GroqRateLimitedError, GroqPermanentError

logger = structlog.get_logger()


class GroqSynthesisUnavailable(RuntimeError):
    """Signals that synthesis failed after the client's bounded retries."""

SYNTHESIS_PROMPT = """You are a senior software engineer reviewing a pull request.

The new code being reviewed:
```
{hunk}
```

Other changed hunks from the same file, including imports and definitions:
```
{changed_context}
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

Check the full same-file changed context before asserting that a symbol is missing, undefined, unimported, or untyped. Imports or definitions elsewhere in the changed file count. Do not report a concern contradicted by that context. Report one concern once even if multiple hunks repeat the same root cause; mention the affected locations together. Do not invent a defect to fill the response. A similar past comment is not sufficient evidence on its own.

Every concern must cite at least one relevant source comment using source_indices. Omit a concern if no supplied historical comment directly supports it. Return an empty "concerns" array when no historically supported concern applies.

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
    similar_comments: List[Dict[str, Any]],
    changed_context: str = "",
    request_context: dict = None,
) -> List[Dict[str, Any]]:
    if not similar_comments:
        logger.info("no_comments_to_synthesize")
        return []

    # Retrieval order is relevance order. Bound only historical evidence; the
    # primary hunk remains intact so critical changed code is never truncated.
    selected_comments = []
    evidence_chars = 0
    for comment in similar_comments[:settings.groq_max_evidence_comments_per_group]:
        line = f"[{len(selected_comments)}] [{float(comment.get('similarity', 0)):.2f} similarity] {comment.get('body', '')} (from {comment.get('path', '')})"
        remaining = settings.groq_max_evidence_chars_per_group - evidence_chars - (1 if selected_comments else 0)
        if remaining <= 0:
            break
        line = line[:remaining]
        selected_comments.append((comment, line))
        evidence_chars += len(line) + 1
    comments_text = "\n".join(line for _, line in selected_comments)
    changed_context = changed_context[:settings.groq_max_changed_context_chars]

    prompt = SYNTHESIS_PROMPT.format(
        hunk=hunk,
        comments=comments_text,
        changed_context=changed_context or "(No other changed hunk was available.)",
    )

    try:
        result = await call_groq_json(prompt, log_context=request_context)
    except (GroqRateLimitedError, GroqPermanentError):
        raise
    if result is None:
        logger.error("synthesis_failed")
        raise GroqSynthesisUnavailable("Groq did not return a synthesis result")

    raw_feedback = result.get("concerns", [])
    if not isinstance(raw_feedback, list):
        logger.warning("synthesis_concerns_invalid_shape")
        return []

    # A concern with no text can't be embedded, matched, judged, or rendered
    # downstream — drop it here so every consumer can assume concern is non-empty.
    feedback = []
    for raw_item in raw_feedback:
        if not isinstance(raw_item, dict):
            continue
        concern = raw_item.get("concern")
        evidence = raw_item.get("evidence")
        if not isinstance(concern, str) or not concern.strip():
            continue
        indices = raw_item.get("source_indices", [])
        if not isinstance(indices, list):
            continue
        sources = [
            selected_comments[i][0]
            for i in indices
            if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(selected_comments)
        ]
        # A finding with no verified source reference is unsupported output.
        if not sources or not isinstance(evidence, str) or not evidence.strip():
            logger.warning("synthesis_unsupported_concern_dropped")
            continue
        item = dict(raw_item)
        item["concern"] = concern.strip()
        item["evidence"] = evidence.strip()
        item["source_comments"] = sources
        confidence = item.get("confidence")
        try:
            confidence = float(confidence)
            item["confidence"] = confidence if math.isfinite(confidence) and 0 <= confidence <= 1 else None
        except (TypeError, ValueError):
            item["confidence"] = None
        item["is_inference"] = item.get("is_inference") is True
        feedback.append(item)

    logger.info(
        "synthesis_complete",
        input_comments=len(similar_comments),
        output_concerns=len(feedback)
    )
    return feedback
