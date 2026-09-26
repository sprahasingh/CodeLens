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
