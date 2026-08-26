import json
import structlog
from google import genai
from typing import List, Dict, Any
from app.core.config import settings

logger = structlog.get_logger()

SCORER_MODEL = "gemini-3.6-flash"

SCORING_PROMPT = """Score the following code review concerns by severity.
{concerns}
Respond with a JSON array where each item has:
- concern: the original concern text
- severity: high, medium, or low
- rationale: one sentence explaining the severity rating
Respond with ONLY valid JSON."""


async def score_concerns(
    concerns: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not concerns:
        logger.info("no_concerns_to_score")
        return []

    concerns_text = "\n".join([
        f"- {c.get('concern', '')} (confidence: {c.get('confidence', 0):.0%})"
        for c in concerns
    ])

    prompt = SCORING_PROMPT.format(concerns=concerns_text)

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = client.models.generate_content(
            model=SCORER_MODEL,
            contents=prompt
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        logger.info("scoring_complete", count=len(result))
        return result
    except Exception as e:
        logger.error("scoring_failed", error=str(e))
        return []