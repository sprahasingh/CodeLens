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


class GroqRateLimitedError(RuntimeError):
    """A temporary provider limit that should be retried by the task queue."""

    def __init__(self, retry_after_seconds: float, category: str = "rate_limited"):
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        self.category = category
        super().__init__(category)


class GroqPermanentError(RuntimeError):
    """A non-retryable provider rejection; message contains no provider text."""

    def __init__(self, status_code: int, category: str):
        self.status_code = status_code
        self.category = category
        super().__init__(category)


def _provider_category(response) -> str:
    try:
        error = response.json().get("error", {})
        code = str(error.get("code", "")).lower()
        kind = str(error.get("type", "")).lower()
    except (ValueError, AttributeError, TypeError):
        code = kind = ""
    if any(marker in f"{code} {kind}" for marker in ("quota", "billing", "insufficient_quota")):
        return "quota_exhausted"
    if any(marker in f"{code} {kind}" for marker in ("auth", "api_key", "permission")):
        return "authentication_or_permission"
    return "rate_limited"


def _safe_rate_headers(headers) -> dict:
    allowed = (
        "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests", "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens",
    )
    return {key: headers[key] for key in allowed if headers.get(key) is not None}


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


async def call_groq_json(prompt: str, model: str = DEFAULT_MODEL, log_context: dict = None) -> Optional[Dict[str, Any]]:
    """Call Groq with bounded retries and free-tier-friendly request pacing.

    Short Retry-After waits are handled in-call. Longer waits are surfaced as
    GroqRateLimitedError so Celery can defer without tying up its worker.
    """
    started = time.monotonic()
    remaining = _cooldown_remaining()
    if remaining:
        logger.warning("groq_cooldown_deferred", **{
            **(log_context or {}), "model": model, "http_status": 429,
            "provider_error_category": "rate_limited",
            "retry_after_seconds": round(remaining, 2),
            "scheduled_delay_seconds": round(remaining, 2),
            "prompt_token_estimate": (len(prompt) + 3) // 4,
            "retry_count": 0, "cumulative_retry_seconds": 0.0,
        })
        raise GroqRateLimitedError(remaining)

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
                        category = _provider_category(response)
                        fields = {
                            **(log_context or {}), "model": model,
                            "http_status": 429, "provider_error_category": category,
                            "retry_after_seconds": retry_after,
                            "rate_limit_headers": _safe_rate_headers(response.headers),
                            "prompt_token_estimate": (len(prompt) + 3) // 4,
                            "retry_count": attempt - 1,
                            "cumulative_retry_seconds": round(time.monotonic() - started, 2),
                        }
                        if category in ("quota_exhausted", "authentication_or_permission"):
                            logger.error("groq_request_permanently_rejected", **fields)
                            raise GroqPermanentError(429, category)
                        delay = retry_after if retry_after is not None else settings.groq_rate_limit_cooldown_seconds
                        _set_cooldown(delay)
                        if delay > settings.groq_retry_max_seconds:
                            logger.warning("groq_deferred_rate_limit", **fields, scheduled_delay_seconds=delay)
                            raise GroqRateLimitedError(delay, category)
                        if attempt > retry_count:
                            logger.warning("groq_rate_limit_retries_exhausted", **fields)
                            raise GroqRateLimitedError(delay, category)
                        wait = max(_backoff(attempt), retry_after or 0.0)
                        logger.warning(
                            "groq_rate_limited_retrying",
                            **(log_context or {}),
                            model=model,
                            attempt=attempt,
                            wait_seconds=round(wait, 2),
                            retry_after_seconds=retry_after,
                            cumulative_retry_seconds=round(time.monotonic() - started + wait, 2),
                            prompt_token_estimate=(len(prompt) + 3) // 4,
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

                    if response.status_code in (401, 403):
                        logger.error("groq_request_permanently_rejected", **{
                            **(log_context or {}), "model": model,
                            "http_status": response.status_code,
                            "provider_error_category": "authentication_or_permission",
                            "prompt_token_estimate": (len(prompt) + 3) // 4,
                        })
                        raise GroqPermanentError(response.status_code, "authentication_or_permission")
                    if 400 <= response.status_code < 500:
                        logger.error("groq_request_permanently_rejected", **{
                            **(log_context or {}), "model": model,
                            "http_status": response.status_code,
                            "provider_error_category": "invalid_request",
                            "prompt_token_estimate": (len(prompt) + 3) // 4,
                        })
                        raise GroqPermanentError(response.status_code, "invalid_request")

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
                    logger.error("groq_call_failed", **{
                        **(log_context or {}), "http_status": exc.response.status_code,
                        "model": model, "provider_error_category": "http_error",
                    })
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
