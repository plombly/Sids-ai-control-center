import importlib.util
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).with_name("local-diagnostic.py")
SPEC = importlib.util.spec_from_file_location("local_diagnostic", SCRIPT)
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)


def test_main_passes_when_all_checks_pass(monkeypatch, capsys):
    checks = ["redis", "orchestrator", "worker", "repository", "api"]
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
    assert output.count("[PASS]") == 4


def test_redis_check_uses_fixed_local_endpoint(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="PONG\n", stderr="")

    monkeypatch.setattr(diagnostic.subprocess, "run", fake_run)

    assert diagnostic.check_redis()[0] is True
    assert calls[0][0] == ["redis-cli", "-h", "127.0.0.1", "-p", "6379", "ping"]
