import httpx
import pytest

import app.services.github_comment as github_comment
from app.core.config import settings


class FakeClient:
    def __init__(self, post_responses):
        self.post_responses = list(post_responses)
        self.post_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        return httpx.Response(200, json=[], request=httpx.Request("GET", "https://api.github.com"))

    async def post(self, *_args, **_kwargs):
        self.post_calls += 1
        response = self.post_responses.pop(0)
        return response

    async def patch(self, *_args, **_kwargs):
        raise AssertionError("no existing marker comment in this test")


def response(status_code, *, headers=None, payload=None):
    return httpx.Response(
        status_code,
        headers=headers,
        json=payload if payload is not None else {"html_url": "https://github.com/o/r/pull/1#issuecomment-1"},
        request=httpx.Request("POST", "https://api.github.com"),
    )


@pytest.mark.asyncio
async def test_github_comment_retries_429_and_honors_retry_after(monkeypatch):
    client = FakeClient([
        response(429, headers={"Retry-After": "2"}),
        response(201),
    ])
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    async def get_client(*_args):
        return client

    async def save(*_args):
        return None

    monkeypatch.setattr(github_comment, "get_github_client", get_client)
    monkeypatch.setattr(github_comment, "save_predictions", save)
    monkeypatch.setattr(github_comment.asyncio, "sleep", sleep)
    monkeypatch.setattr(github_comment.random, "uniform", lambda _a, _b: 0.5)
    monkeypatch.setattr(settings, "github_comment_max_retries", 1)
    monkeypatch.setattr(settings, "github_comment_retry_base_seconds", 1.0)
    monkeypatch.setattr(settings, "github_comment_retry_max_seconds", 10.0)

    posted = await github_comment.post_pr_comment("o", "r", 1, [], head_sha="sha")

    assert posted is True
    assert client.post_calls == 2
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_github_comment_retry_exhaustion_is_bounded(monkeypatch):
    client = FakeClient([response(503), response(503)])
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    async def get_client(*_args):
        return client

    monkeypatch.setattr(github_comment, "get_github_client", get_client)
    monkeypatch.setattr(github_comment, "save_predictions", lambda *_args: None)
    monkeypatch.setattr(github_comment.asyncio, "sleep", sleep)
    monkeypatch.setattr(github_comment.random, "uniform", lambda _a, _b: 0.5)
    monkeypatch.setattr(settings, "github_comment_max_retries", 1)
    monkeypatch.setattr(settings, "github_comment_retry_base_seconds", 0.0)
    monkeypatch.setattr(settings, "github_comment_retry_max_seconds", 1.0)

    posted = await github_comment.post_pr_comment("o", "r", 1, [], head_sha="sha")

    assert posted is False
    assert client.post_calls == 2
    assert len(sleeps) == 1
