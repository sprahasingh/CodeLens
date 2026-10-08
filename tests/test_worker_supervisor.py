import app.core.worker_supervisor as supervisor
from app.core.config import settings


def test_unresponsive_worker_is_replaced_when_broker_is_reachable(monkeypatch):
    class Child:
        pid = 123
        returncode = None

        def poll(self):
            return self.returncode

    child = Child()
    stop_calls = []
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(supervisor, "_broker_available", lambda: True)
    monkeypatch.setattr(supervisor, "_worker_replies", lambda: False)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(settings, "worker_health_check_seconds", 5)
    monkeypatch.setattr(settings, "worker_health_failures_before_restart", 3)

    def stop(process, grace_seconds=20):
        if process.returncode is None:
            stop_calls.append(process.pid)
        process.returncode = -15
        monkeypatch.setattr(supervisor, "stopping", True)

    monkeypatch.setattr(supervisor, "_stop_child", stop)
    supervisor.stopping = False
    assert supervisor.run() == 0
    assert len(stop_calls) == 1


def test_docker_health_check_fails_when_worker_ping_is_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "HEALTH_FILE", tmp_path / "healthy")
    assert not supervisor._healthy_file_is_fresh()


def test_disk_warning_only_logs_on_crossing_threshold(monkeypatch):
    class Usage:
        total = 100
        used = 86
        free = 14

    logged = []
    monkeypatch.setattr(supervisor.shutil, "disk_usage", lambda _path: Usage())
    monkeypatch.setattr(settings, "worker_disk_warning_percent", 85)
    monkeypatch.setattr(supervisor.logger, "warning", lambda *args, **kwargs: logged.append((args, kwargs)))

    assert supervisor._check_disk_usage(False) is True
    assert supervisor._check_disk_usage(True) is True
    assert len(logged) == 1


def test_stable_worker_run_resets_crash_backoff(monkeypatch):
    monkeypatch.setattr(settings, "worker_restart_stability_seconds", 600)
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 700)
    assert supervisor._worker_run_was_stable(100) is True
    assert supervisor._worker_run_was_stable(101) is False
