"""Secret-safe failure handling tests for the production deployment helper."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "deploy_ghcr.sh"
SECRET = "canary-secret-do-not-log"


DOCKER_MOCK = r'''#!/usr/bin/env bash
set -u
mode="${MOCK_MODE:-celery_failure}"
if [[ -n "${MOCK_CALLS:-}" ]]; then
  case "$1:$2:${*:3}" in
    compose:*up*) printf 'compose_up\\n' >>"$MOCK_CALLS" ;;
    pull:*) printf 'docker_pull\\n' >>"$MOCK_CALLS" ;;
    image:tag:*) printf 'image_tag\\n' >>"$MOCK_CALLS" ;;
    exec:*) printf 'docker_exec\\n' >>"$MOCK_CALLS" ;;
    *) printf 'other\\n' >>"$MOCK_CALLS" ;;
  esac
fi
if [[ "$1" == compose ]]; then
  args=" $* "
  if [[ "$args" == *" version --short "* ]]; then
    printf '5.5.1\n'
  elif [[ "$args" == *" config --services "* ]]; then
    printf 'web\nworker\nflower\ncaddy\n'
  elif [[ "$args" == *" ps -q "* ]]; then
    case "$args" in
      *" ps -q web "*) printf 'web-container\n' ;;
      *" ps -q worker "*) printf 'worker-container\n' ;;
      *" ps -q flower "*) printf 'flower-container\n' ;;
      *) exit 1 ;;
    esac
  elif [[ "$args" == *" up "* ]]; then
    printf '%s\n' "$MOCK_SECRET" >&2
    exit 1
  fi
  exit 0
fi
if [[ "$1" == exec ]]; then
  case "$mode" in
    celery_failure)
      printf '%s\n' "$MOCK_SECRET"
      printf '%s\n' "$MOCK_SECRET" >&2
      exit 1
      ;;
    celery_malformed)
      printf 'inspection=%s\n' "$MOCK_SECRET"
      exit 0
      ;;
    celery_busy)
      printf '%s\n' "${MOCK_COUNTS:-active=1 reserved=0 scheduled=0 unacked=0 unacked_index=0}"
      exit 0
      ;;
    celery_idle)
      printf 'active=0 reserved=0 scheduled=0 unacked=0 unacked_index=0\n'
      exit 0
      ;;
    login_failure)
      printf 'active=0 reserved=0 scheduled=0 unacked=0 unacked_index=0\n'
      exit 0
      ;;
    pull_failure|compose_failure)
      printf 'active=0 reserved=0 scheduled=0 unacked=0 unacked_index=0\n'
      exit 0
      ;;
  esac
fi
if [[ "$1" == image && "$2" == tag ]]; then
  exit 0
fi
if [[ "$1" == image && "$2" == inspect ]]; then
  case "$*" in
    *'org.opencontainers.image.revision'*) printf '%s\n' "$EXPECTED_SHA" ;;
    *'{{.Os}}/{{.Architecture}}'*) printf 'linux/amd64\n' ;;
    *) printf 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n' ;;
  esac
  exit 0
fi
if [[ "$1" == login ]]; then
  cat >/dev/null
  printf '%s\n' "$MOCK_SECRET" >&2
  printf '%s\n' "$MOCK_SECRET"
  printf '%s' "$MOCK_SECRET" >"$DOCKER_CONFIG/config.json"
  if [[ "$mode" == login_failure ]]; then
    exit 1
  fi
  exit 0
fi
if [[ "$1" == pull ]]; then
  printf '%s\n' "$MOCK_SECRET" >&2
  printf '%s\n' "$MOCK_SECRET"
  [[ "$mode" != pull_failure ]] && exit 0
  exit 1
fi
if [[ "$1" == inspect ]]; then
  case "$*" in
    *'{{.Image}}'*) printf 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n' ;;
    *'{{.State.Health.Status}}'*) printf 'healthy\n' ;;
  esac
  exit 0
fi
printf '%s\n' "$MOCK_SECRET" >&2
exit 1
'''


@pytest.fixture
def deployment_sandbox(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    docker = mock_bin / "docker"
    docker.write_text(DOCKER_MOCK)
    docker.chmod(0o700)
    flock = mock_bin / "flock"
    flock.write_text("#!/usr/bin/env bash\nexit 0\n")
    flock.chmod(0o700)
    timeout = mock_bin / "timeout"
    timeout.write_text("#!/usr/bin/env bash\nshift\nexec \"$@\"\n")
    timeout.chmod(0o700)

    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    (compose_dir / ".env").write_text("test-only-placeholder\n")
    (compose_dir / "codelens-spraha.2026-08-18.private-key.pem").write_text("test-only-placeholder\n")
    (compose_dir / "docker-compose.yml").write_text("services: {}\n")
    (compose_dir / "docker-compose.prod.yml").write_text("services: {}\n")
    override = tmp_path / "override.yml"
    override.write_text("services: {}\n")

    temp_root = tmp_path / "temporary"
    temp_root.mkdir()
    state_dir = tmp_path / "state"
    env = {
        "PATH": f"{mock_bin}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(temp_root),
        "CODELENS_COMPOSE_DIR": str(compose_dir),
        "CODELENS_STATE_DIR": str(state_dir),
        "CODELENS_MIN_FREE_MIB": "1",
        "CODELENS_POST_PULL_FREE_MIB": "1",
        "CODELENS_PUBLIC_HEALTH_URL": "",
        "CODELENS_DEPLOYMENT_ID": "test-deployment",
        "EXPECTED_SHA": "1" * 40,
    "MOCK_SECRET": SECRET,
    "MOCK_MODE": "celery_failure",
    "MOCK_CALLS": str(tmp_path / "mock-calls.txt"),
    }
    return env, override, temp_root


def run_deploy(
    env: dict[str, str], override: Path, *, mode: str, token_path: Path
) -> subprocess.CompletedProcess[str]:
    token_path.write_text("test-only-token\n")
    token_path.chmod(0o600)
    child_env = {**env, "MOCK_MODE": mode}
    return subprocess.run(
        [
            "bash",
            str(HELPER),
            "deploy",
            "ghcr.io/example/codelens@sha256:" + "a" * 64,
            "1" * 40,
            "test-user",
            str(token_path),
            str(override),
        ],
        env=child_env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def assert_secret_not_logged(result: subprocess.CompletedProcess[str]) -> None:
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr


def test_celery_command_failure_redacts_stdout_and_stderr_and_fails_closed(
    deployment_sandbox: tuple[dict[str, str], Path, Path], tmp_path: Path
) -> None:
    env, override, _ = deployment_sandbox
    token = Path("/tmp") / f"codelens-ghcr-token-test-{os.getpid()}"
    try:
        result = run_deploy(env, override, mode="celery_failure", token_path=token)
        assert result.returncode != 0
        assert_secret_not_logged(result)
        assert "Celery task inspection failed; deployment aborted." in result.stderr
        assert "docker compose" not in result.stderr
        calls = Path(env["MOCK_CALLS"]).read_text()
        assert "docker_pull" not in calls
        assert "image_tag" not in calls
        assert "compose_up" not in calls
        assert not token.exists()
    finally:
        token.unlink(missing_ok=True)


def test_unexpected_celery_output_is_rejected_without_echoing_it(
    deployment_sandbox: tuple[dict[str, str], Path, Path]
) -> None:
    env, override, _ = deployment_sandbox
    token = Path("/tmp") / f"codelens-ghcr-token-test-{os.getpid()}-malformed"
    try:
        result = run_deploy(env, override, mode="celery_malformed", token_path=token)
        assert result.returncode != 0
        assert_secret_not_logged(result)
        assert "unexpected output" in result.stderr
        assert not token.exists()
    finally:
        token.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "counts",
    [
        "active=1 reserved=0 scheduled=0 unacked=0 unacked_index=0",
        "active=0 reserved=1 scheduled=0 unacked=0 unacked_index=0",
        "active=0 reserved=0 scheduled=1 unacked=0 unacked_index=0",
        "active=0 reserved=0 scheduled=0 unacked=1 unacked_index=0",
        "active=0 reserved=0 scheduled=0 unacked=0 unacked_index=1",
    ],
)
def test_busy_celery_state_reports_only_allowlisted_counts(
    deployment_sandbox: tuple[dict[str, str], Path, Path], counts: str
) -> None:
    env, override, _ = deployment_sandbox
    env["MOCK_COUNTS"] = counts
    token = Path("/tmp") / f"codelens-ghcr-token-test-{os.getpid()}-busy"
    try:
        result = run_deploy(env, override, mode="celery_busy", token_path=token)
        assert result.returncode != 0
        assert_secret_not_logged(result)
        assert counts in result.stderr
        assert "image" not in result.stderr
        assert not token.exists()
    finally:
        token.unlink(missing_ok=True)


def test_ghcr_login_failure_redacts_output_and_removes_temporary_credentials(
    deployment_sandbox: tuple[dict[str, str], Path, Path]
) -> None:
    env, override, temp_root = deployment_sandbox
    token = Path("/tmp") / f"codelens-ghcr-token-test-{os.getpid()}-login"
    try:
        result = run_deploy(env, override, mode="login_failure", token_path=token)
        assert result.returncode != 0
        assert_secret_not_logged(result)
        assert "GHCR authentication failed." in result.stderr
        assert not token.exists()
        assert list(temp_root.glob("codelens-ghcr-auth.*")) == []
        for config_file in temp_root.glob("**/config.json"):
            assert SECRET not in config_file.read_text()
        assert not any("STOP" in line and SECRET in line for line in result.stderr.splitlines())
    finally:
        token.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("mode", "expected_message"),
    [
        ("pull_failure", "unable to pull the pinned GHCR image."),
        ("compose_failure", "Deployment update failed; restoring the recorded image IDs."),
    ],
)
def test_later_deployment_failures_redact_output_and_clean_auth_files(
    deployment_sandbox: tuple[dict[str, str], Path, Path], mode: str, expected_message: str
) -> None:
    env, override, temp_root = deployment_sandbox
    token = Path("/tmp") / f"codelens-ghcr-token-test-{os.getpid()}-{mode}"
    try:
        result = run_deploy(env, override, mode=mode, token_path=token)
        assert result.returncode != 0
        assert_secret_not_logged(result)
        assert expected_message in result.stderr
        assert not token.exists()
        assert list(temp_root.glob("codelens-ghcr-auth.*")) == []
    finally:
        token.unlink(missing_ok=True)
