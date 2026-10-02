import importlib.util
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).with_name("local-diagnostic.py")
SPEC = importlib.util.spec_from_file_location("local_diagnostic", SCRIPT)
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)


WORKERS = ("laika-worker@01.service", "laika-worker@02.service")


def test_main_passes_when_all_checks_pass(monkeypatch, capsys):
    monkeypatch.setattr(diagnostic, "worker_services", lambda: WORKERS)
    checks = ["redis", "orchestrator", *WORKERS, "repository", "api"]
    monkeypatch.setattr(
        diagnostic,
        "check_redis",
        lambda: (True, "Redis responded to PING"),
    )
    monkeypatch.setattr(
        diagnostic,
        "check_service",
        lambda label, service: (True, f"{label} is active ({service})"),
    )
    monkeypatch.setattr(
        diagnostic,
        "check_repository",
        lambda: (True, "repository status is clean"),
    )
    monkeypatch.setattr(
        diagnostic,
        "check_api",
        lambda: (True, "API health endpoint is healthy"),
    )

    assert diagnostic.main() == 0
    output = capsys.readouterr().out
    assert output.count("[PASS]") == len(checks)


def test_main_fails_and_reports_failed_check(monkeypatch, capsys):
    monkeypatch.setattr(diagnostic, "worker_services", lambda: WORKERS)
    monkeypatch.setattr(
        diagnostic,
        "check_redis",
        lambda: (False, "Redis: connection refused"),
    )
    monkeypatch.setattr(diagnostic, "check_service", lambda *_: (True, "active"))
    monkeypatch.setattr(diagnostic, "check_repository", lambda: (True, "clean"))
    monkeypatch.setattr(diagnostic, "check_api", lambda: (True, "healthy"))

    assert diagnostic.main() == 1
    output = capsys.readouterr().out
    assert "[FAIL] Redis: connection refused" in output
    assert output.count("[PASS]") == len(WORKERS) + 3


def test_redis_check_uses_fixed_local_endpoint(monkeypatch):
    calls = []

    class FakeRedis:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def ping(self):
            return True

    monkeypatch.setattr(diagnostic.redis, "Redis", FakeRedis)

    assert diagnostic.check_redis()[0] is True
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 6379

def test_service_check_reports_systemd_state(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")

    monkeypatch.setattr(diagnostic.subprocess, "run", fake_run)

    passed, detail = diagnostic.check_service("worker", "laika-worker.service")

    assert passed is False
    assert "inactive" in detail
    assert calls == [["systemctl", "is-active", "laika-worker.service"]]


def test_worker_services_follow_the_scaler(monkeypatch):
    class FakeRedis:
        def __init__(self, **kwargs):
            pass

        def get(self, key):
            return "3" if key == "laika:scaler:target" else None
    monkeypatch.delenv("LAIKA_WORKER_SERVICES", raising=False)
    monkeypatch.setattr(diagnostic.redis, "Redis", FakeRedis)
    assert diagnostic.worker_services() == ("laika-worker@01.service", "laika-worker@02.service", "laika-worker@03.service")
    monkeypatch.setenv("LAIKA_WORKER_SERVICES", "a.service, b.service")
    assert diagnostic.worker_services() == ("a.service", "b.service")
