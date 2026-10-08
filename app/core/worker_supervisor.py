"""Run Celery and recover only when its broker is healthy but worker is not."""
import os
import signal
import subprocess
import sys
import time
import shutil
from pathlib import Path

import structlog

from app.core.celery_app import celery_app
from app.core.config import settings

logger = structlog.get_logger()
HEALTH_FILE = Path("/tmp/celery-worker-healthy")
stopping = False


def _handle_signal(_signum, _frame):
    global stopping
    stopping = True


def _broker_available() -> bool:
    connection = celery_app.connection_for_read()
    try:
        connection.ensure_connection(max_retries=0)
        return True
    except Exception as exc:
        logger.warning("worker_health_broker_unavailable", error_type=type(exc).__name__)
        return False
    finally:
        try:
            connection.release()
        except Exception:
            pass


def _worker_replies() -> bool:
    try:
        replies = celery_app.control.ping(
            timeout=settings.worker_health_check_timeout_seconds,
            limit=1,
        )
        return bool(replies)
    except Exception as exc:
        logger.warning("worker_health_ping_failed", error_type=type(exc).__name__)
        return False


def _healthy_file_is_fresh() -> bool:
    try:
        return time.time() - HEALTH_FILE.stat().st_mtime <= settings.worker_health_check_seconds * 4
    except OSError:
        return False


def _check_disk_usage(warning_active: bool) -> bool:
    """Warn once when the container's backing filesystem crosses its threshold."""
    usage = shutil.disk_usage("/")
    percent = usage.used * 100 // usage.total
    if percent >= settings.worker_disk_warning_percent and not warning_active:
        logger.warning(
            "worker_disk_usage_high",
            used_percent=percent,
            available_bytes=usage.free,
            warning_percent=settings.worker_disk_warning_percent,
        )
        return True
    if percent < settings.worker_disk_warning_percent:
        return False
    return warning_active


def _sleep_or_stop(seconds: int) -> None:
    """Keep shutdown responsive while applying worker restart backoff."""
    for _ in range(seconds):
        if stopping:
            return
        time.sleep(1)


def _worker_run_was_stable(started_at: float) -> bool:
    return time.monotonic() - started_at >= settings.worker_restart_stability_seconds


def _reconcile_due_reviews() -> int:
    from app.tasks.pr_tasks import reconcile_review_jobs
    return reconcile_review_jobs()


def _stop_child(child: subprocess.Popen, grace_seconds: int = 20) -> None:
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
    except ProcessLookupError:
        pass


def run() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        return 0 if _healthy_file_is_fresh() else 1

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "app.core.celery_app",
        "worker",
        "--loglevel=info",
        f"--concurrency={settings.groq_max_concurrency}",
        "--prefetch-multiplier=1",
    ]
    restart_delay = 5
    disk_warning_active = False
    consecutive_exits = 0
    last_reconcile = 0.0

    while not stopping:
        child_started_at = time.monotonic()
        child = subprocess.Popen(command, start_new_session=True)
        logger.info("celery_worker_supervisor_started", pid=child.pid)
        missed_pings = 0

        while not stopping and child.poll() is None:
            time.sleep(settings.worker_health_check_seconds)
            disk_warning_active = _check_disk_usage(disk_warning_active)
            if stopping or child.poll() is not None:
                break

            if not _broker_available():
                # Celery's own infinite broker reconnect is allowed to recover an
                # Upstash or network outage; restarting cannot fix unavailable Redis.
                missed_pings = 0
                continue

            now = time.monotonic()
            if now - last_reconcile >= settings.review_reconcile_interval_seconds:
                try:
                    recovered = _reconcile_due_reviews()
                    if recovered:
                        logger.warning("review_reconciliation_dispatched", count=recovered)
                except Exception as exc:
                    logger.error("review_reconciliation_failed", error_type=type(exc).__name__)
                last_reconcile = now

            if _worker_replies():
                missed_pings = 0
                try:
                    HEALTH_FILE.touch()
                except OSError:
                    pass
                continue

            missed_pings += 1
            logger.warning(
                "celery_worker_health_miss",
                missed_pings=missed_pings,
                failures_before_restart=settings.worker_health_failures_before_restart,
            )
            if missed_pings >= settings.worker_health_failures_before_restart:
                logger.error("celery_worker_unresponsive_restarting", pid=child.pid)
                _stop_child(child)
                break

        if stopping:
            _stop_child(child, grace_seconds=settings.worker_shutdown_grace_seconds)
            break

        exit_code = child.poll()
        logger.error("celery_worker_process_exited", exit_code=exit_code)
        if stopping:
            break
        if _worker_run_was_stable(child_started_at):
            consecutive_exits = 0
            restart_delay = 5
        consecutive_exits += 1
        if consecutive_exits >= 5:
            logger.critical(
                "celery_worker_repeated_crash_backoff",
                consecutive_exits=consecutive_exits,
                cooldown_seconds=300,
            )
            _sleep_or_stop(300)
            consecutive_exits = 0
            restart_delay = 30
            continue
        _sleep_or_stop(restart_delay)
        restart_delay = min(restart_delay * 2, 300)

    HEALTH_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
