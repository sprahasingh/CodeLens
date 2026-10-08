import asyncio
import email.utils
import json
import random
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import httpx
import structlog

from app.core.config import settings

logger = structlog.get_logger()

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"

_request_gate = asyncio.Semaphore(settings.groq_max_concurrency)
_cooldown_lock = threading.Lock()
_cooldown_until = 0.0


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _set_cooldown(seconds: float) -> None:
    global _cooldown_until
    with _cooldown_lock:
        _cooldown_until = max(_cooldown_until, time.monotonic() + max(0.0, seconds))


def _cooldown_remaining() -> float:
    with _cooldown_lock:
        return max(0.0, _cooldown_until - time.monotonic())


def _backoff(attempt: int) -> float:
    ceiling = min(
        settings.groq_retry_max_seconds,
        settings.groq_retry_base_seconds * (2 ** max(0, attempt - 1)),
    )
    return random.uniform(0.0, ceiling)


async def call_groq_json(prompt: str, model: str = DEFAULT_MODEL) -> Optional[Dict[str, Any]]:
    """Call Groq with bounded retries and free-tier-friendly request pacing.

    A Retry-After value is treated as the minimum wait. If it exceeds the
    configured retry window, this call stops and a process-local cooldown
    prevents subsequent hunks from making more requests during that window.
    """
    remaining = _cooldown_remaining()
    if remaining:
        logger.warning("groq_cooldown_active", remaining_seconds=round(remaining, 1))
        return None

    retry_count = settings.groq_max_retries
    timeout = httpx.Timeout(settings.groq_timeout_seconds)
    async with _request_gate:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, retry_count + 2):
                try:
                    response = await client.post(
                        GROQ_CHAT_URL,
                        headers={
                            "Authorization": f"Bearer {settings.groq_api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "response_format": {"type": "json_object"},
                            "temperature": 0,
                        },
                    )

                    if response.status_code == 429:
                        retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                        if retry_after is not None:
                            _set_cooldown(retry_after)
                        if attempt > retry_count:
                            logger.warning("groq_rate_limit_retries_exhausted", attempts=attempt)
                            _set_cooldown(
                                retry_after if retry_after is not None
                                else settings.groq_rate_limit_cooldown_seconds
                            )
                            return None

                        wait_cap = settings.groq_retry_max_seconds
                        if retry_after is not None and retry_after > wait_cap:
                            logger.warning(
                                "groq_retry_after_exceeds_window",
                                retry_after_seconds=round(retry_after, 1),
                                retry_window_seconds=wait_cap,
                            )
                            _set_cooldown(retry_after)
                            return None
                        wait = max(_backoff(attempt), retry_after or 0.0)
                        logger.warning(
                            "groq_rate_limited_retrying",
                            attempt=attempt,
                            wait_seconds=round(wait, 2),
                        )
                        await asyncio.sleep(wait)
                        continue

                    if response.status_code >= 500:
                        if attempt > retry_count:
                            logger.error("groq_server_retries_exhausted", attempts=attempt)
                            return None
                        wait = _backoff(attempt)
                        logger.warning(
                            "groq_server_error_retrying",
                            status_code=response.status_code,
                            attempt=attempt,
                            wait_seconds=round(wait, 2),
                        )
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
                        total_tokens=usage.get("total_tokens"),
                    )
                    raw = body["choices"][0]["message"]["content"].strip()
                    return json.loads(raw)
                except httpx.TimeoutException:
                    if attempt > retry_count:
                        logger.error("groq_timeout_retries_exhausted", attempts=attempt)
                        return None
                    wait = _backoff(attempt)
                    logger.warning("groq_timeout_retrying", attempt=attempt, wait_seconds=round(wait, 2))
                    await asyncio.sleep(wait)
                except httpx.HTTPStatusError as exc:
                    logger.error("groq_call_failed", status_code=exc.response.status_code)
                    return None
                except httpx.RequestError as exc:
                    if attempt > retry_count:
                        logger.error("groq_network_retries_exhausted", error_type=type(exc).__name__)
                        return None
                    wait = _backoff(attempt)
                    logger.warning(
                        "groq_network_retrying",
                        error_type=type(exc).__name__,
                        attempt=attempt,
                        wait_seconds=round(wait, 2),
                    )
                    await asyncio.sleep(wait)
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    logger.error("groq_response_invalid", error_type=type(exc).__name__)
                    return None

    return None
