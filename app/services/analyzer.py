import json
import structlog
from google import genai
from typing import List, Dict, Any
from app.core.config import settings

logger = structlog.get_logger()

client = genai.Client(api_key=settings.gemini_api_key)

ANALYSIS_MODEL = "gemini-3.6-flash"

ANALYSIS_PROMPT = """Analyze the following code and identify potential issues.
{code}
Respond with a JSON array of issues found."""


async def analyze_code(
    hunks: List[str],
    repo_owner: str,
    repo_name: str,
    limit: int = 5
) -> List[Dict[str, Any]]:
    if not hunks:
        logger.info("no_hunks_to_analyze")
        return []

    code_text = "\n".join(hunks)
    prompt = ANALYSIS_PROMPT.format(code=code_text)

    try:
        response = client.models.generate_content(
            model=ANALYSIS_MODEL,
            contents=prompt
        )
        raw = response.text.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        results = json.loads(raw)
        logger.info("analysis_complete", concerns=len(results))
        return results

    except Exception as e:
        logger.error("analysis_failed", error=str(e))
        return []