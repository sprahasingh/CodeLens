import pytest
from pydantic import ValidationError

import app.services.llm_client as llm_client
from app.core.config import Settings, settings


class FakeResponse:
    def __init__(self, status_code, headers=None, body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body or {"choices": [{"message": {"content": '{"concerns": []}'}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            request = httpx.Request("POST", "https://api.groq.com")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("failed", request=request, response=response)

    def json(self):
        return self._body


class FakeClient:
    responses = []
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        type(self).calls += 1
        return type(self).responses.pop(0)


def test_groq_concurrency_configuration_cannot_exceed_one():
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+asyncpg://test:test@127.0.0.1/test",
            github_app_id=1,
            github_installation_id=1,
            redis_url="redis://127.0.0.1:6379/0",
            voyage_api_key="test",
            groq_api_key="test",
            groq_max_concurrency=2,
        )


@pytest.mark.asyncio
async def test_429_honors_retry_after_and_succeeds(monkeypatch):
    FakeClient.responses = [FakeResponse(429, {"Retry-After": "2"}), FakeResponse(200)]
    FakeClient.calls = 0
    sleeps = []
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(llm_client.asyncio, "sleep", lambda seconds: _record_sleep(sleeps, seconds))
    monkeypatch.setattr(llm_client.random, "uniform", lambda low, high: 0.25)
    monkeypatch.setattr(llm_client, "_cooldown_until", 0.0)
    monkeypatch.setattr(settings, "groq_max_retries", 2)
    monkeypatch.setattr(settings, "groq_retry_max_seconds", 5.0)

    result = await llm_client.call_groq_json("prompt")

    assert result == {"concerns": []}
    assert FakeClient.calls == 2
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_multiple_short_429_responses_then_success_are_bounded(monkeypatch):
    FakeClient.responses = [FakeResponse(429, {"Retry-After": "1"}),
                            FakeResponse(429, {"Retry-After": "2"}), FakeResponse(200)]
    FakeClient.calls = 0
    sleeps = []
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(llm_client.asyncio, "sleep", lambda seconds: _record_sleep(sleeps, seconds))
    monkeypatch.setattr(llm_client.random, "uniform", lambda low, high: 0.0)
    monkeypatch.setattr(llm_client, "_cooldown_until", 0.0)
    monkeypatch.setattr(settings, "groq_max_retries", 2)
    monkeypatch.setattr(settings, "groq_retry_max_seconds", 5.0)

    assert await llm_client.call_groq_json("prompt") == {"concerns": []}
    assert FakeClient.calls == 3
    assert sleeps == [1.0, 2.0]


@pytest.mark.asyncio
async def test_429_attempts_are_bounded_and_future_call_can_recover(monkeypatch):
    FakeClient.responses = [FakeResponse(429), FakeResponse(429), FakeResponse(429)]
    FakeClient.calls = 0
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeClient)
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(llm_client.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(llm_client.random, "uniform", lambda low, high: 0.0)
    monkeypatch.setattr(llm_client, "_cooldown_until", 0.0)
    monkeypatch.setattr(llm_client.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(settings, "groq_max_retries", 2)
    monkeypatch.setattr(settings, "groq_rate_limit_cooldown_seconds", 10.0)

    with pytest.raises(llm_client.GroqRateLimitedError):
        await llm_client.call_groq_json("prompt")
    assert FakeClient.calls == 3
    assert llm_client._cooldown_remaining() == 10.0

    FakeClient.responses = [FakeResponse(200)]
    with pytest.raises(llm_client.GroqRateLimitedError):
        await llm_client.call_groq_json("still in cooldown")
    assert FakeClient.calls == 3
    monkeypatch.setattr(llm_client.time, "monotonic", lambda: 111.0)
    assert await llm_client.call_groq_json("next PR") == {"concerns": []}
    assert FakeClient.calls == 4


@pytest.mark.asyncio
async def test_retry_after_beyond_configured_window_stops_without_extra_request(monkeypatch):
    FakeClient.responses = [FakeResponse(429, {"Retry-After": "90"})]
    FakeClient.calls = 0
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(llm_client, "_cooldown_until", 0.0)
    monkeypatch.setattr(settings, "groq_retry_max_seconds", 10.0)

    with pytest.raises(llm_client.GroqRateLimitedError) as exc:
        await llm_client.call_groq_json("prompt")
    assert exc.value.retry_after_seconds == 90
    assert FakeClient.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [32, 45])
async def test_long_retry_after_is_deferred_without_sleep_or_second_request(monkeypatch, seconds):
    FakeClient.responses = [FakeResponse(429, {"Retry-After": str(seconds)})]
    FakeClient.calls = 0
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(llm_client, "_cooldown_until", 0.0)
    monkeypatch.setattr(settings, "groq_retry_max_seconds", 20.0)

    with pytest.raises(llm_client.GroqRateLimitedError) as exc:
        await llm_client.call_groq_json("private prompt")
    assert exc.value.retry_after_seconds == seconds
    assert FakeClient.calls == 1


@pytest.mark.asyncio
async def test_quota_exhaustion_is_not_retried(monkeypatch):
    FakeClient.responses = [FakeResponse(429, {"Retry-After": "45"}, {"error": {"code": "insufficient_quota"}})]
    FakeClient.calls = 0
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(llm_client, "_cooldown_until", 0.0)

    with pytest.raises(llm_client.GroqPermanentError) as exc:
        await llm_client.call_groq_json("private prompt")
    assert exc.value.category == "quota_exhausted"
    assert FakeClient.calls == 1


@pytest.mark.asyncio
async def test_authentication_rejection_is_not_deferred(monkeypatch):
    FakeClient.responses = [FakeResponse(429, {"Retry-After": "45"}, {"error": {"code": "invalid_api_key"}})]
    FakeClient.calls = 0
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(llm_client, "_cooldown_until", 0.0)

    with pytest.raises(llm_client.GroqPermanentError) as exc:
        await llm_client.call_groq_json("private prompt")
    assert exc.value.category == "authentication_or_permission"
    assert FakeClient.calls == 1


@pytest.mark.asyncio
async def test_rate_limit_logs_are_structured_and_do_not_include_prompt(monkeypatch):
    secret_prompt = "TOP_SECRET_REPOSITORY_SOURCE"
    FakeClient.responses = [FakeResponse(429, {
        "Retry-After": "32", "x-ratelimit-remaining-tokens": "0",
        "x-ratelimit-reset-tokens": "32s",
    })]
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(llm_client, "_cooldown_until", 0.0)
    captured = []

    class Logger:
        def warning(self, event, **fields):
            captured.append((event, fields))

        def error(self, event, **fields):
            captured.append((event, fields))

    monkeypatch.setattr(llm_client, "logger", Logger())
    with pytest.raises(llm_client.GroqRateLimitedError):
        await llm_client.call_groq_json(secret_prompt, log_context={"pr_number": 10, "synthesis_group_id": "group"})

    rendered = repr(captured)
    assert secret_prompt not in rendered
    assert "prompt_token_estimate" in rendered
    assert "rate_limit_headers" in rendered


async def _record_sleep(sleeps, seconds):
    sleeps.append(seconds)
