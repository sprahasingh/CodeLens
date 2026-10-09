"""Offline tests for trusted, digest-pinned GHCR publication verification."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError

import pytest

from scripts import verify_ghcr_publication as verifier


SOURCE_COMMIT = "b3c7a3cc74336272b907c10eb32ca874aaa908cb"
IMAGE_DIGEST = "sha256:" + "a" * 64
GITHUB_TOKEN = "synthetic-github-token-canary"


class FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body


def response_json(value: dict[str, object], headers: dict[str, str] | None = None) -> FakeResponse:
    return FakeResponse(json.dumps(value, separators=(",", ":")).encode(), headers)


def registry_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    architecture: str = "amd64",
    label_commit: str = SOURCE_COMMIT,
) -> str:
    config = {
        "os": "linux",
        "architecture": architecture,
        "config": {"Labels": {"org.opencontainers.image.revision": label_commit}},
    }
    config_body = json.dumps(config, separators=(",", ":")).encode()
    config_digest = "sha256:" + hashlib.sha256(config_body).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": config_digest},
        "layers": [],
    }
    manifest_body = json.dumps(manifest, separators=(",", ":")).encode()
    manifest_digest = "sha256:" + hashlib.sha256(manifest_body).hexdigest()

    run = {
        "path": ".github/workflows/production.yml@refs/heads/main",
        "repository": {"full_name": "sprahasingh/CodeLens"},
        "head_branch": "main",
        "head_sha": SOURCE_COMMIT,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
    }
    jobs = {
        "jobs": [
            {"name": "test", "conclusion": "success", "steps": []},
            {
                "name": "publish",
                "conclusion": "success",
                "steps": [
                    {"name": "Run full suite inside the exact production image", "conclusion": "success"},
                    {"name": "Push the exact tested image to GHCR", "conclusion": "success"},
                ],
            },
        ]
    }

    def fake_urlopen(request: object, timeout: int = 0) -> FakeResponse:
        url = request.full_url  # type: ignore[attr-defined]
        if url.startswith("https://api.github.com/repos/sprahasingh/CodeLens/actions/runs/"):
            return response_json(jobs if "/jobs?" in url else run)
        if url.startswith("https://ghcr.io/token?"):
            return response_json({"token": "synthetic-registry-token"})
        if url == f"https://ghcr.io/v2/sprahasingh/codelens/manifests/{SOURCE_COMMIT}":
            return FakeResponse(manifest_body, {"Docker-Content-Digest": manifest_digest})
        if url == f"https://ghcr.io/v2/sprahasingh/codelens/blobs/{config_digest}":
            return FakeResponse(config_body, {"Docker-Content-Digest": config_digest})
        raise AssertionError("unexpected verification request")

    monkeypatch.setattr(verifier, "urlopen", fake_urlopen)
    return manifest_digest


def test_success_selects_only_the_fixed_repository_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = registry_fixture(monkeypatch)
    image = verifier.verify_publication(
        publication_run_id="9",
        source_commit=SOURCE_COMMIT,
        image_digest=digest,
        github_token=GITHUB_TOKEN,
        username="test-actor",
    )
    assert image == f"ghcr.io/sprahasingh/codelens@{digest}"


@pytest.mark.parametrize(
    ("source_commit", "image_digest"),
    [
        (SOURCE_COMMIT, "sha256:invalid"),
        (SOURCE_COMMIT, f"ghcr.io/other/repo@{IMAGE_DIGEST}"),
    ],
)
def test_invalid_digest_or_unrelated_reference_is_rejected_without_network(
    monkeypatch: pytest.MonkeyPatch, source_commit: str, image_digest: str
) -> None:
    def unexpected_request(*_: object, **__: object) -> None:
        raise AssertionError("input validation must happen before network access")

    monkeypatch.setattr(verifier, "urlopen", unexpected_request)
    with pytest.raises(verifier.VerificationError):
        verifier.verify_publication(
            publication_run_id="9",
            source_commit=source_commit,
            image_digest=image_digest,
            github_token=GITHUB_TOKEN,
            username="test-actor",
        )


def test_digest_must_match_registry_content(monkeypatch: pytest.MonkeyPatch) -> None:
    registry_fixture(monkeypatch)
    with pytest.raises(verifier.VerificationError):
        verifier.verify_publication(
            publication_run_id="9",
            source_commit=SOURCE_COMMIT,
            image_digest="sha256:" + "b" * 64,
            github_token=GITHUB_TOKEN,
            username="test-actor",
        )


def test_source_commit_must_match_successful_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = registry_fixture(monkeypatch)
    with pytest.raises(verifier.VerificationError):
        verifier.verify_publication(
            publication_run_id="9",
            source_commit="0" * 40,
            image_digest=digest,
            github_token=GITHUB_TOKEN,
            username="test-actor",
        )


def test_unsupported_architecture_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = registry_fixture(monkeypatch, architecture="arm64")
    with pytest.raises(verifier.VerificationError):
        verifier.verify_publication(
            publication_run_id="9",
            source_commit=SOURCE_COMMIT,
            image_digest=digest,
            github_token=GITHUB_TOKEN,
            username="test-actor",
        )


def test_image_label_must_match_source_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = registry_fixture(monkeypatch, label_commit="0" * 40)
    with pytest.raises(verifier.VerificationError):
        verifier.verify_publication(
            publication_run_id="9",
            source_commit=SOURCE_COMMIT,
            image_digest=digest,
            github_token=GITHUB_TOKEN,
            username="test-actor",
        )


@pytest.mark.parametrize("failure", ["failed_run", "untrusted_event", "failed_publish_step"])
def test_failed_or_untrusted_publication_is_rejected(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    digest = registry_fixture(monkeypatch)
    original = verifier._github_json

    def changed_github_json(url: str, token: str) -> dict[str, object]:
        payload = original(url, token)
        if "/jobs?" in url and failure == "failed_publish_step":
            payload["jobs"][1]["conclusion"] = "failure"  # type: ignore[index]
        elif "/jobs?" not in url and failure == "failed_run":
            payload["conclusion"] = "failure"
        elif "/jobs?" not in url and failure == "untrusted_event":
            payload["event"] = "pull_request"
        return payload

    monkeypatch.setattr(verifier, "_github_json", changed_github_json)
    with pytest.raises(verifier.VerificationError):
        verifier.verify_publication(
            publication_run_id="9",
            source_commit=SOURCE_COMMIT,
            image_digest=digest,
            github_token=GITHUB_TOKEN,
            username="test-actor",
        )


def test_validation_failure_does_not_log_secret_or_provider_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def failed_request(*_: object, **__: object) -> None:
        raise HTTPError("https://api.github.com", 403, GITHUB_TOKEN, {}, None)

    monkeypatch.setattr(verifier, "urlopen", failed_request)
    output_file = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)
    monkeypatch.setenv("GITHUB_ACTOR", "test-actor")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    assert verifier.main(
        ["--publication-run-id", "9", "--source-commit", SOURCE_COMMIT, "--image-digest", IMAGE_DIGEST]
    ) == 1
    captured = capsys.readouterr()
    assert "EXISTING_IMAGE_VERIFICATION: FAIL" in captured.err
    assert GITHUB_TOKEN not in captured.out + captured.err
    assert "403" not in captured.out + captured.err


def test_workflow_existing_mode_is_manual_gated_and_does_not_publish() -> None:
    workflow_file = Path(
        os.environ.get(
            "CODELENS_WORKFLOW_FILE",
            Path(__file__).resolve().parents[1] / ".github/workflows/production.yml",
        )
    )
    workflow = workflow_file.read_text()
    assert "options: [deploy, deploy_existing, rollback]" in workflow
    assert "if: github.event_name != 'workflow_dispatch' || inputs.operation == 'deploy' || inputs.operation == 'deploy_existing'" in workflow
    assert "verify_existing:" in workflow
    assert "if: github.event_name == 'workflow_dispatch' && inputs.operation == 'deploy_existing' && github.ref == 'refs/heads/main'" in workflow
    assert "needs.verify_existing.result == 'success'" in workflow
    assert "environment: production" in workflow
    assert "inputs.operation == 'deploy_existing'" not in workflow.split("  publish:", 1)[1].split("  verify_existing:", 1)[0]
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "vars.CODELENS_AUTO_DEPLOY == 'true'" in workflow
    assert "  rollback:\n    if: github.event_name == 'workflow_dispatch' && inputs.operation == 'rollback'" in workflow
