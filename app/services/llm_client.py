import asyncio
import hashlib
import json
import httpx
import structlog
from redis import asyncio as redis_async
from redis.exceptions import RedisError
from typing import Optional, Dict, Any
from app.core.config import settings

logger = structlog.get_logger()

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"
MAX_RETRIES = 2


async def _claim_groq_request_slot() -> bool:
    key_suffix = hashlib.sha256(settings.groq_api_key.encode()).hexdigest()[:16]
    key = f"codelens:groq:request-slot:{key_suffix}"
    try:
        async with redis_async.from_url(settings.redis_url) as client:
            return bool(await client.set(
                key,
                "1",
                ex=settings.groq_min_request_interval_seconds,
                nx=True
            ))
    except RedisError as exc:
        logger.warning("groq_rate_limiter_unavailable", error=str(exc))
        return True


async def call_groq_json(prompt: str, model: str = DEFAULT_MODEL) -> Optional[Dict[str, Any]]:
    """Call Groq's OpenAI-compatible chat completions endpoint in JSON mode
    and return the parsed response object. Every prompt using this helper
    must ask for a JSON *object* at the root (e.g. {"concerns": [...]}), not
    a bare array — JSON mode is not guaranteed to accept an array root.
    Returns None on any failure; callers decide the empty-result fallback."""
    if not await _claim_groq_request_slot():
        logger.warning("groq_rate_limited_locally_skipping")
        return None

    for attempt in range(1, MAX_RETRIES + 1):
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
                if response.status_code == 429:
                    retry_after = response.headers.get("retry-after", "1")
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = 3
                    if attempt == MAX_RETRIES or wait > 2:
                        logger.warning(
                            "groq_rate_limited_skipping",
                            attempt=attempt,
                            retry_after=retry_after
                        )
                        return None
                    logger.warning("groq_rate_limited_retrying", attempt=attempt, wait_seconds=wait)
                    await asyncio.sleep(wait)
                    continue
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
    logger.error("groq_rate_limit_exhausted_retries", attempts=MAX_RETRIES)
    return None
