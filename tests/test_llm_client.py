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
    monkeypatch.setattr(settings, "groq_rate_limit_cooldown_seconds", 30.0)

    assert await llm_client.call_groq_json("prompt") is None
    assert FakeClient.calls == 3
    assert llm_client._cooldown_remaining() == 30.0

    FakeClient.responses = [FakeResponse(200)]
    assert await llm_client.call_groq_json("still in cooldown") is None
    assert FakeClient.calls == 3
    monkeypatch.setattr(llm_client.time, "monotonic", lambda: 131.0)
    assert await llm_client.call_groq_json("next PR") == {"concerns": []}
    assert FakeClient.calls == 4


@pytest.mark.asyncio
async def test_retry_after_beyond_configured_window_stops_without_extra_request(monkeypatch):
    FakeClient.responses = [FakeResponse(429, {"Retry-After": "90"})]
    FakeClient.calls = 0
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(llm_client, "_cooldown_until", 0.0)
    monkeypatch.setattr(settings, "groq_retry_max_seconds", 10.0)

    assert await llm_client.call_groq_json("prompt") is None
    assert FakeClient.calls == 1


async def _record_sleep(sleeps, seconds):
    sleeps.append(seconds)
