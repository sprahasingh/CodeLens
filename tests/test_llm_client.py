import app.services.llm_client as llm_client


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "{\"ok\": true}"}}]}


class FakeAsyncClient:
    responses = []
    requests = 0

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        type(self).requests += 1
        return type(self).responses.pop(0)


async def test_claim_groq_request_slot_uses_shared_expiring_redis_key(monkeypatch):
    calls = []

    class FakeRedisClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def set(self, key, value, ex, nx):
            calls.append((key, value, ex, nx))
            return True

    monkeypatch.setattr(llm_client.redis_async, "from_url", lambda url: FakeRedisClient())

    assert await llm_client._claim_groq_request_slot() is True
    assert calls[0][0].startswith("codelens:groq:request-slot:")
    assert calls[0][1:] == ("1", llm_client.settings.groq_min_request_interval_seconds, True)


async def test_call_groq_json_skips_when_shared_slot_is_unavailable(monkeypatch):
    async def deny_slot():
        return False

    monkeypatch.setattr(llm_client, "_claim_groq_request_slot", deny_slot)
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.requests = 0

    assert await llm_client.call_groq_json("prompt") is None
    assert FakeAsyncClient.requests == 0


async def test_call_groq_json_retries_once_only_for_short_retry_after(monkeypatch):
    async def allow_slot():
        return True

    async def no_wait(_):
        return None

    monkeypatch.setattr(llm_client, "_claim_groq_request_slot", allow_slot)
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(llm_client.asyncio, "sleep", no_wait)
    FakeAsyncClient.responses = [
        FakeResponse(429, {"retry-after": "1"}),
        FakeResponse(200),
    ]
    FakeAsyncClient.requests = 0

    assert await llm_client.call_groq_json("prompt") == {"ok": True}
    assert FakeAsyncClient.requests == 2


async def test_call_groq_json_does_not_wait_for_long_rate_limit(monkeypatch):
    async def allow_slot():
        return True

    monkeypatch.setattr(llm_client, "_claim_groq_request_slot", allow_slot)
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.responses = [FakeResponse(429, {"retry-after": "30"})]
    FakeAsyncClient.requests = 0

    assert await llm_client.call_groq_json("prompt") is None
    assert FakeAsyncClient.requests == 1
