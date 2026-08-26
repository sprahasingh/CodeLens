import json
import structlog
from google import genai
from typing import List, Dict, Any
from app.core.config import settings

logger = structlog.get_logger()

FORMATTER_MODEL = "gemini-3.6-flash"

FORMAT_PROMPT = """You are a technical writer formatting code review feedback.
Given the following concerns identified in a pull request:
{concerns}

Format them into a clear, developer-friendly summary.
Respond with a JSON array where each item has:
- title: short title (5 words max)
- description: expanded explanation (2-3 sentences)
- priority: high, medium, or low

Respond with ONLY valid JSON. No markdown, no preamble."""


async def format_concerns(
    concerns: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not concerns:
        logger.info("no_concerns_to_format")
        return []

    concerns_text = "\n".join([
        f"- {c.get('concern', '')} (confidence: {c.get('confidence', 0):.0%})"
        for c in concerns
    ])

    prompt = FORMAT_PROMPT.format(concerns=concerns_text)

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = client.models.generate_content(
            model=FORMATTER_MODEL,
            contents=prompt
        )
        raw = response.text.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = json.loads(raw)
        logger.info("formatting_complete", count=len(result))
        return result

    except Exception as e:
        logger.error("formatting_failed", error=str(e))
        return []