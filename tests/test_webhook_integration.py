import hmac
import hashlib
import json
from fastapi.testclient import TestClient
from app.main import app
from app.core.config import settings

client = TestClient(app)


def test_health_check_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def _sign(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_webhook_ping_with_valid_signature_returns_pong(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "test-secret")
    payload = json.dumps({"zen": "keep it logically awesome"}).encode()
    signature = _sign(payload, "test-secret")

    response = client.post(
        "/webhook",
        content=payload,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "ping",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "pong"}


def test_webhook_rejects_invalid_signature(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "test-secret")
    payload = json.dumps({"zen": "keep it logically awesome"}).encode()

    response = client.post(
        "/webhook",
        content=payload,
        headers={
            "X-Hub-Signature-256": "sha256=deadbeefdeadbeefdeadbeefdeadbeef",
            "X-GitHub-Event": "ping",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 401


def test_webhook_ignores_unhandled_event_types(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "test-secret")
    payload = json.dumps({"anything": "here"}).encode()
    signature = _sign(payload, "test-secret")

    response = client.post(
        "/webhook",
        content=payload,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "some_unhandled_event",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


def test_duplicate_pr_webhook_queues_only_once(monkeypatch):
    from app.routers import webhook

    claims = iter([True, False])
    queued = []

    async def fake_claim(*args):
        return next(claims)

    monkeypatch.setattr(webhook, "claim_pr_processing", fake_claim)
    monkeypatch.setattr(webhook.process_pr, "apply_async", lambda **kwargs: queued.append(kwargs))
    monkeypatch.setattr(settings, "webhook_secret", "")
    payload = {
        "action": "opened",
        "repository": {"name": "repo", "owner": {"login": "owner"}},
        "pull_request": {"number": 17, "head": {"sha": "abc123"}},
    }
    headers = {"X-GitHub-Event": "pull_request", "Content-Type": "application/json"}

    first = client.post("/webhook", json=payload, headers=headers)
    second = client.post("/webhook", json=payload, headers=headers)

    assert first.status_code == 200 and first.json()["status"] == "queued"
    assert second.status_code == 200 and second.json()["status"] == "duplicate_skipped"
    assert len(queued) == 1
    assert queued[0]["kwargs"]["expected_head_sha"] == "abc123"
