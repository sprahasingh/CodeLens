import json
import httpx
import structlog
from typing import Optional, Dict, Any
from app.core.config import settings

logger = structlog.get_logger()

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"


async def call_groq_json(prompt: str, model: str = DEFAULT_MODEL) -> Optional[Dict[str, Any]]:
    """Call Groq's OpenAI-compatible chat completions endpoint in JSON mode
    and return the parsed response object. Every prompt using this helper
    must ask for a JSON *object* at the root (e.g. {"concerns": [...]}), not
    a bare array — JSON mode is not guaranteed to accept an array root.
    Returns None on any failure; callers decide the empty-result fallback."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                GROQ_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0
                }
            )
            response.raise_for_status()
            body = response.json()
            usage = body.get("usage", {})
            logger.info(
                "groq_call_usage",
                model=model,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens")
            )
            raw = body["choices"][0]["message"]["content"].strip()
            return json.loads(raw)
    except httpx.HTTPStatusError as e:
        logger.error(
            "groq_call_failed",
            status_code=e.response.status_code,
            body=e.response.text[:500]
        )
        return None
    except Exception as e:
        logger.error("groq_call_failed", error=str(e))
        return None
