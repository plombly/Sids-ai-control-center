import pytest

from sid_testing import MemoryRedis, ROOT, load_module


module = load_module(ROOT / "services/worker/worker.py")
module.redis = MemoryRedis()


class FakeStdin:
    def write(self, _prompt):
        pass

    def close(self):
        pass


class FakeProcess:
    stdin = FakeStdin()
    pid = 1
    returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def run_fake_codex(monkeypatch, tmp_path):
    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return FakeProcess()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module, "heartbeat", lambda _status: None)

    def run():
        module.run_codex(
            {"id": "envtest", "role": "builder", "prompt": "hi"},
            tmp_path,
            tmp_path / "codex.jsonl",
        )
        assert len(calls) == 1
        command, env = calls[0]
        assert command[:2] == ["codex", "exec"]
        return env

    return run


def test_run_codex_keeps_process_home(run_fake_codex, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert run_fake_codex()["HOME"] == str(tmp_path / "home")


def test_run_codex_falls_back_to_root_when_home_missing(run_fake_codex, monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    assert run_fake_codex()["HOME"] == "/root"


def test_run_codex_falls_back_to_root_when_home_empty(run_fake_codex, monkeypatch):
    monkeypatch.setenv("HOME", "")
    assert run_fake_codex()["HOME"] == "/root"
