#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PROJECT="${CODELENS_COMPOSE_PROJECT:-codelens}"
COMPOSE_DIR="${CODELENS_COMPOSE_DIR:-/home/ubuntu/CodeLens-deploy-6ce5119}"
STATE_DIR="${CODELENS_STATE_DIR:-/home/ubuntu/.local/state/codelens-ghcr}"
MIN_FREE_MIB="${CODELENS_MIN_FREE_MIB:-1536}"
POST_PULL_FREE_MIB="${CODELENS_POST_PULL_FREE_MIB:-300}"
PUBLIC_HEALTH_URL="${CODELENS_PUBLIC_HEALTH_URL:-https://13-51-158-45.sslip.io/health}"
AUTH_TMP_ROOT="${TMPDIR:-/tmp}"
AUTH_TOKEN_FILE=""
AUTH_DOCKER_CONFIG=""

cleanup_auth() {
  local cleanup_failed=0 token_name exit_status=$?

  if [[ -n "${AUTH_DOCKER_CONFIG:-}" ]]; then
    case "$AUTH_DOCKER_CONFIG" in
      "$AUTH_TMP_ROOT"/codelens-ghcr-auth.*)
        rm -rf -- "$AUTH_DOCKER_CONFIG" >/dev/null 2>&1 || cleanup_failed=1
        ;;
      *) cleanup_failed=1 ;;
    esac
  fi

  if [[ -n "${AUTH_TOKEN_FILE:-}" ]]; then
    token_name="${AUTH_TOKEN_FILE##*/}"
    if [[ "$AUTH_TOKEN_FILE" == "/tmp/$token_name" && "$token_name" =~ ^codelens-ghcr-token-[A-Za-z0-9._-]+$ ]]; then
      rm -f -- "$AUTH_TOKEN_FILE" >/dev/null 2>&1 || cleanup_failed=1
    else
      cleanup_failed=1
    fi
  fi

  if (( cleanup_failed )); then
    printf 'WARNING: temporary authentication cleanup was incomplete.\n' >&2
    (( exit_status != 0 )) || exit_status=1
  fi
  trap - EXIT
  exit "$exit_status"
}

trap cleanup_auth EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

usage() {
  cat <<'USAGE'
Usage:
  deploy_ghcr.sh deploy IMAGE@sha256:DIGEST EXPECTED_COMMIT GHCR_USERNAME TOKEN_FILE OVERRIDE_FILE
  deploy_ghcr.sh rollback DEPLOYMENT_ID|latest OVERRIDE_FILE

Deployment uses a GHCR image digest, never builds on EC2, and only recreates
web, worker, and Flower in the existing codelens Compose project.
USAGE
}

compose_base() {
  local override="$1"
  shift
  CODELENS_IMAGE="${CODELENS_IMAGE:-ghcr.io/placeholder/codelens@sha256:0000000000000000000000000000000000000000000000000000000000000000}" docker compose -p "$PROJECT" \
    -f "$COMPOSE_DIR/docker-compose.yml" \
    -f "$COMPOSE_DIR/docker-compose.prod.yml" \
    -f "$override" "$@"
}

free_kib() {
  df -Pk "$COMPOSE_DIR" | awk 'NR == 2 { print $4 }'
}

require_free_mib() {
  local min_mib="$1" current_kib
  current_kib="$(free_kib)"
  if [[ ! "$current_kib" =~ ^[0-9]+$ ]] || (( current_kib < min_mib * 1024 )); then
    printf 'STOP: available space is %s MiB; required minimum is %s MiB.\n' \
      "$((current_kib / 1024))" "$min_mib" >&2
    return 1
  fi
}

check_preconditions() {
  local compose_version services
  [[ -d "$COMPOSE_DIR" ]] || { echo "STOP: Compose directory is missing." >&2; return 1; }
  [[ -f "$COMPOSE_DIR/.env" ]] || { echo "STOP: production env file is missing." >&2; return 1; }
  [[ -f "$COMPOSE_DIR/codelens-spraha.2026-08-18.private-key.pem" ]] || {
    echo "STOP: mounted GitHub App key is missing." >&2; return 1;
  }
  [[ -f "$COMPOSE_DIR/docker-compose.yml" && -f "$COMPOSE_DIR/docker-compose.prod.yml" ]] || {
    echo "STOP: expected Compose files are missing." >&2; return 1;
  }
  compose_version="$(docker compose version --short 2>/dev/null)" || {
    echo "STOP: unable to verify Docker Compose version." >&2; return 1;
  }
  if ! python3 -c 'import re, sys; m=re.match(r"^v?(\d+)\.(\d+)\.(\d+)", sys.argv[1]); raise SystemExit(0 if m and tuple(map(int, m.groups())) >= (2, 24, 4) else 1)' "$compose_version" >/dev/null 2>&1; then
    echo "STOP: Docker Compose 2.24.4 or newer is required for build reset semantics." >&2
    return 1
  fi
  services="$(compose_base "$1" config --services 2>/dev/null | sort | tr '\n' ' ')" || {
    echo "STOP: unable to validate the Compose services." >&2; return 1;
  }
  for service in web worker flower caddy; do
    [[ " $services " == *" $service "* ]] || {
      echo "STOP: expected Compose service '$service' is absent." >&2; return 1;
    }
  done
  for service in web worker flower; do
    local cid
    cid="$(compose_base "$1" ps -q "$service" 2>/dev/null)" || {
      echo "STOP: unable to verify a running application service." >&2; return 1;
    }
    [[ -n "$cid" ]] || { echo "STOP: running $service container was not found." >&2; return 1; }
  done
}

acquire_deployment_lock() {
  command -v flock >/dev/null 2>&1 || {
    echo "STOP: flock is required to prevent overlapping deployment operations." >&2; return 1;
  }
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  exec 9>"$STATE_DIR/deploy.lock"
  if ! flock -n 9; then
    echo "STOP: another deployment operation is running." >&2
    return 1
  fi
}

assert_celery_quiescent() {
  local output active reserved scheduled unacked unacked_index
  if ! output="$(timeout 25s docker exec codelens-worker-1 python -c '
from app.core.celery_app import celery_app
connection = celery_app.connection_for_read()
try:
    client = connection.channel().client
    unacked = client.hlen("unacked")
    unacked_index = client.zcard("unacked_index")
finally:
    connection.release()
inspector = celery_app.control.inspect(timeout=3)
counts = {}
for method in ("active", "reserved", "scheduled"):
    response = getattr(inspector, method)()
    if not isinstance(response, dict) or not response:
        raise SystemExit("worker inspection unavailable: " + method)
    counts[method] = sum(len(items) for items in response.values())
print("active={active} reserved={reserved} scheduled={scheduled} unacked={unacked} unacked_index={unacked_index}".format(**counts, unacked=unacked, unacked_index=unacked_index))
' 2>/dev/null)"; then
    echo "STOP: Celery task inspection failed; deployment aborted." >&2
    return 1
  fi
  if [[ ! "$output" =~ ^active=([0-9]+)[[:space:]]reserved=([0-9]+)[[:space:]]scheduled=([0-9]+)[[:space:]]unacked=([0-9]+)[[:space:]]unacked_index=([0-9]+)$ ]]; then
    echo "STOP: Celery task inspection returned unexpected output; deployment aborted." >&2
    return 1
  fi
  active="${BASH_REMATCH[1]}"
  reserved="${BASH_REMATCH[2]}"
  scheduled="${BASH_REMATCH[3]}"
  unacked="${BASH_REMATCH[4]}"
  unacked_index="${BASH_REMATCH[5]}"
  if (( active || reserved || scheduled || unacked || unacked_index )); then
    printf 'STOP: Celery is not quiescent (active=%s reserved=%s scheduled=%s unacked=%s unacked_index=%s).\n' \
      "$active" "$reserved" "$scheduled" "$unacked" "$unacked_index" >&2
    return 1
  fi
  printf 'Celery task inspection: PASS (active=0 reserved=0 scheduled=0 unacked=0 unacked_index=0).\n'
}

verify_running_release() {
  local deadline status flower_status
  deadline=$((SECONDS + 150))
  while (( SECONDS < deadline )); do
    if curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/health >/dev/null 2>&1; then
      status="$(docker inspect --format '{{.State.Health.Status}}' codelens-worker-1 2>/dev/null || true)"
      flower_status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 http://127.0.0.1:5555/ 2>/dev/null || true)"
      if [[ "$status" == healthy ]] && [[ "$flower_status" =~ ^(200|301|302|401|403)$ ]]; then
        if timeout 15s docker exec codelens-worker-1 python -c \
          'from app.core.celery_app import celery_app; r=celery_app.control.ping(timeout=5); raise SystemExit(0 if r else 1)' >/dev/null 2>&1; then
          if [[ -n "$PUBLIC_HEALTH_URL" ]] && curl --fail --silent --max-time 10 "$PUBLIC_HEALTH_URL" >/dev/null 2>&1; then
            return 0
          fi
          # Public HTTPS can fail transiently while Caddy is serving a new web container.
        fi
      fi
    fi
    sleep 5
  done
  echo "Health verification timed out (web, worker, Celery ping, Flower, or HTTPS)." >&2
  return 1
}

write_rollback_override() {
  local state_file="$1" override_file="$2"
  # State files are generated locally by this script and contain only image IDs/tags.
  # shellcheck disable=SC1090
  source "$state_file"
  cat > "$override_file" <<YAML
services:
  web:
    image: ${WEB_ROLLBACK_TAG}
    build: !reset null
    pull_policy: never
  worker:
    image: ${WORKER_ROLLBACK_TAG}
    build: !reset null
    pull_policy: never
  flower:
    image: ${FLOWER_ROLLBACK_TAG}
    build: !reset null
    pull_policy: never
YAML
  chmod 600 "$override_file"
}

rollback_state() {
  local state_file="$1" override_file="$2"
  [[ -f "$state_file" ]] || { echo "STOP: rollback state file is missing." >&2; return 1; }
  write_rollback_override "$state_file" "$override_file"
  if ! compose_base "$override_file" up -d --no-build --pull never --no-deps web worker flower >/dev/null 2>&1; then
    echo "Rollback Compose operation failed; stop for manual inspection." >&2
    return 1
  fi
  verify_running_release
}

deploy() {
  local image_ref="$1" expected_sha="$2" ghcr_username="$3" token_file="$4" override_file="$5"
  local run_id state_file temp_config web_id worker_id flower_id
  local web_tag worker_tag flower_tag revision platform free_before free_after

  if [[ "$token_file" =~ ^/tmp/codelens-ghcr-token-[A-Za-z0-9._-]+$ ]]; then
    AUTH_TOKEN_FILE="$token_file"
  else
    echo "STOP: invalid short-lived GHCR token file path." >&2; return 1
  fi
  [[ "$image_ref" =~ ^ghcr\.io/[a-z0-9][a-z0-9._-]*/codelens@sha256:[a-f0-9]{64}$ ]] || {
    echo "STOP: image must be a lowercase GHCR repository reference pinned by sha256 digest." >&2; return 1;
  }
  [[ "$expected_sha" =~ ^[a-f0-9]{40}$ ]] || { echo "STOP: expected source commit must be a full SHA." >&2; return 1; }
  [[ -s "$token_file" ]] || { echo "STOP: short-lived GHCR token file is missing." >&2; return 1; }
  [[ -f "$override_file" ]] || { echo "STOP: Compose override is missing." >&2; return 1; }
  export CODELENS_IMAGE="$image_ref"
  acquire_deployment_lock

  check_preconditions "$override_file"
  assert_celery_quiescent
  free_before="$(free_kib)"
  require_free_mib "$MIN_FREE_MIB"
  if (( $(df -Pi "$COMPOSE_DIR" | awk 'NR == 2 { print $4 }') < 10000 )); then
    echo "STOP: fewer than 10,000 free inodes." >&2; return 1
  fi

  run_id="${CODELENS_DEPLOYMENT_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
  [[ "$run_id" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "STOP: invalid deployment id." >&2; return 1; }
  state_file="$STATE_DIR/${run_id}.state"
  [[ ! -e "$state_file" ]] || { echo "STOP: deployment state already exists." >&2; return 1; }
  web_id="$(docker inspect --format '{{.Image}}' "$(compose_base "$override_file" ps -q web 2>/dev/null)" 2>/dev/null)" || {
    echo "STOP: unable to record the running web image for rollback." >&2; return 1;
  }
  worker_id="$(docker inspect --format '{{.Image}}' "$(compose_base "$override_file" ps -q worker 2>/dev/null)" 2>/dev/null)" || {
    echo "STOP: unable to record the running worker image for rollback." >&2; return 1;
  }
  flower_id="$(docker inspect --format '{{.Image}}' "$(compose_base "$override_file" ps -q flower 2>/dev/null)" 2>/dev/null)" || {
    echo "STOP: unable to record the running Flower image for rollback." >&2; return 1;
  }
  web_tag="codelens-web:rollback-${run_id}"
  worker_tag="codelens-worker:rollback-${run_id}"
  flower_tag="codelens-flower:rollback-${run_id}"
  if ! docker image tag "$web_id" "$web_tag" >/dev/null 2>&1; then
    echo "STOP: unable to preserve the web rollback image." >&2; return 1;
  fi
  if ! docker image tag "$worker_id" "$worker_tag" >/dev/null 2>&1; then
    echo "STOP: unable to preserve the worker rollback image." >&2; return 1;
  fi
  if ! docker image tag "$flower_id" "$flower_tag" >/dev/null 2>&1; then
    echo "STOP: unable to preserve the Flower rollback image." >&2; return 1;
  fi
  cat > "$state_file" <<STATE
DEPLOYMENT_ID='$run_id'
IMAGE_REF='$image_ref'
EXPECTED_SHA='$expected_sha'
WEB_IMAGE_ID='$web_id'
WORKER_IMAGE_ID='$worker_id'
FLOWER_IMAGE_ID='$flower_id'
WEB_ROLLBACK_TAG='$web_tag'
WORKER_ROLLBACK_TAG='$worker_tag'
FLOWER_ROLLBACK_TAG='$flower_tag'
STATE
  chmod 600 "$state_file"

  temp_config="$(mktemp -d "$AUTH_TMP_ROOT/codelens-ghcr-auth.XXXXXX" 2>/dev/null)" || {
    echo "STOP: unable to create temporary Docker authentication storage." >&2; return 1;
  }
  AUTH_DOCKER_CONFIG="$temp_config"
  chmod 700 "$temp_config" 2>/dev/null || {
    echo "STOP: unable to secure temporary Docker authentication storage." >&2; return 1;
  }
  export DOCKER_CONFIG="$temp_config"
  if ! docker login ghcr.io --username "$ghcr_username" --password-stdin < "$token_file" >/dev/null 2>&1; then
    echo "STOP: GHCR authentication failed." >&2; return 1
  fi
  if ! rm -f -- "$token_file" >/dev/null 2>&1; then
    echo "STOP: unable to remove the temporary GHCR token file." >&2; return 1
  fi
  AUTH_TOKEN_FILE=""
  if ! docker pull "$image_ref" >/dev/null 2>&1; then
    echo "STOP: unable to pull the pinned GHCR image." >&2; return 1
  fi
  free_after="$(free_kib)"
  if (( free_after < POST_PULL_FREE_MIB * 1024 )); then
    echo "STOP: image pulled but free space fell below the post-pull safety floor; services were not changed." >&2
    return 1
  fi
  revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image_ref" 2>/dev/null)" || {
    echo "STOP: unable to verify the published image revision." >&2; return 1;
  }
  platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image_ref" 2>/dev/null)" || {
    echo "STOP: unable to verify the published image platform." >&2; return 1;
  }
  [[ "$revision" == "$expected_sha" ]] || { echo "STOP: image revision label does not match the requested commit." >&2; return 1; }
  [[ "$platform" == linux/amd64 ]] || { echo "STOP: image platform does not match EC2 linux/amd64." >&2; return 1; }
  if ! compose_base "$override_file" config --quiet >/dev/null 2>&1; then
    echo "STOP: the deployment Compose configuration is invalid." >&2; return 1;
  fi

  if ! compose_base "$override_file" up -d --no-build --pull never --no-deps web worker flower >/dev/null 2>&1; then
    echo "Deployment update failed; restoring the recorded image IDs." >&2
    rollback_state "$state_file" "$override_file" || true
    return 1
  fi
  if ! verify_running_release; then
    echo "Post-deploy health check failed; restoring the recorded image IDs." >&2
    rollback_state "$state_file" "$override_file" || true
    return 1
  fi
  ln -sfn "$state_file" "$STATE_DIR/latest"
  printf 'Deployment: PASS. Image digest: %s\n' "${image_ref##*@}"
  printf 'Free space: before=%s MiB after=%s MiB\n' "$((free_before / 1024))" "$((free_after / 1024))"
}

rollback() {
  local run_id="$1" override_file="$2" state_file
  acquire_deployment_lock
  if [[ "$run_id" == latest ]]; then
    [[ -L "$STATE_DIR/latest" ]] || { echo "STOP: no successful deployment state is available." >&2; return 1; }
    state_file="$(readlink -f "$STATE_DIR/latest")"
  else
    [[ "$run_id" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "STOP: invalid deployment id." >&2; return 1; }
    state_file="$STATE_DIR/${run_id}.state"
  fi
  rollback_state "$state_file" "$override_file"
  printf 'Rollback: PASS. Saved image IDs are healthy.\n'
}

main() {
  case "${1:-}" in
    deploy)
      [[ $# -eq 6 ]] || { usage >&2; return 2; }
      shift
      deploy "$@"
      ;;
    rollback)
      [[ $# -eq 3 ]] || { usage >&2; return 2; }
      shift
      rollback "$@"
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage >&2
      return 2
      ;;
  esac
}

main "$@"
