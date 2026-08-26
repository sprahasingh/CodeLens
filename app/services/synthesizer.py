import json
import structlog
from google import genai
from google.genai import types
from typing import List, Dict, Any
from app.core.config import settings

logger = structlog.get_logger()

client = genai.Client(api_key=settings.gemini_api_key)

SYNTHESIS_MODEL = "gemini-3.6-flash"

SYNTHESIS_PROMPT = """You are a senior software engineer reviewing a pull request.
You have been given a list of similar past review comments that were left on code similar to what appears in this new PR.

Your job is to synthesize these past comments into clear, actionable pre-review feedback for the developer.

Past review comments retrieved:
{comments}

For each distinct concern you identify, respond with a JSON array where each item has exactly these fields:
- concern: brief description of the issue (1 sentence)
- evidence: what in the past reviews suggests this concern (1 sentence)
- confidence: float between 0.0 and 1.0 indicating how confident you are
- suggested_check: specific thing the developer should verify (1 sentence)
- is_inference: true if this is inferred from patterns, false if directly stated

Respond with ONLY a valid JSON array. No preamble, no explanation, no markdown.
Example format:
[
  {{
    "concern": "Missing error handling for database queries",
    "evidence": "Past reviewers flagged similar database calls that returned None silently",
    "confidence": 0.85,
    "suggested_check": "Verify that None return values are handled explicitly",
    "is_inference": false
  }}
]"""


async def synthesize_feedback(
    similar_comments: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not similar_comments:
        logger.info("no_comments_to_synthesize")
        return []

    comments_text = "\n".join([
        f"- [{c['similarity']:.2f} similarity] {c['body']} (from {c['path']})"
        for c in similar_comments
    ])

    prompt = SYNTHESIS_PROMPT.format(comments=comments_text)

    try:
        response = client.models.generate_content(
            model=SYNTHESIS_MODEL,
            contents=prompt
        )
        raw = response.text.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        feedback = json.loads(raw)

        logger.info(
            "synthesis_complete",
            input_comments=len(similar_comments),
            output_concerns=len(feedback)
        )
        return feedback

    except Exception as e:
        logger.error("synthesis_failed", error=str(e))
        return []