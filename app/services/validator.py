import json
import structlog
from google import genai
from typing import List, Dict, Any
from app.core.config import settings

logger = structlog.get_logger()

VALIDATOR_MODEL = "gemini-3.6-flash"

VALIDATION_PROMPT = """Validate the following code review feedback for completeness.
{feedback}
Respond with a JSON array of validation results."""


async def validate_feedback(
    feedback: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not feedback:
        logger.info("no_feedback_to_validate")
        return []

    feedback_text = "\n".join([
        f"- {f.get('concern', '')} (confidence: {f.get('confidence', 0):.0%})"
        for f in feedback
    ])

    prompt = VALIDATION_PROMPT.format(feedback=feedback_text)

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = client.models.generate_content(
            model=VALIDATOR_MODEL,
            contents=prompt
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        logger.info("validation_complete", count=len(result))
        return result
    except Exception as e:
        logger.error("validation_failed", error=str(e))
        return []