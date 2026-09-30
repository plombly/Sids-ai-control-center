import json
import types

import pytest

from sid_testing import MemoryRedis, ROOT, load_module


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    module = load_module(ROOT / "services/orchestrator/orchestrator.py")
    module.r = MemoryRedis()
    module.REPO_ROOT = tmp_path
    module.planner_prompt = lambda goal, atomic=False: "planner prompt"
    captured = {}

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return types.SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": '{"summary": "s", "jobs": []}',
                },
            }),
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return module, captured


def test_planner_passes_home_and_other_environment(orchestrator, tmp_path, monkeypatch):
    module, captured = orchestrator
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SID_TEST_MARKER", "x")

    module.run_codex_planner("goal")

    assert captured["env"]["HOME"] == str(tmp_path / "home")
    assert captured["env"]["SID_TEST_MARKER"] == "x"


def test_planner_uses_root_when_home_is_unset(orchestrator, monkeypatch):
    module, captured = orchestrator
    monkeypatch.delenv("HOME", raising=False)

    module.run_codex_planner("goal")

    assert captured["env"]["HOME"] == "/root"


def test_planner_uses_root_when_home_is_empty(orchestrator, monkeypatch):
    module, captured = orchestrator
    monkeypatch.setenv("HOME", "")

    module.run_codex_planner("goal")

    assert captured["env"]["HOME"] == "/root"
