"""Live job logs: Codex and Claude events normalized, read incrementally."""

import json

import pytest
from fastapi.testclient import TestClient

import log_routes
import main
from test_project_routes import FakeRedis


def test_codex_and_claude_events_are_normalized():
    codex = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Adding the page"}},
        {"type": "item.started", "item": {"type": "command_execution", "command": "pytest -q"}},
        {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "1 failed", "exit_code": 1}},
        {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py", "kind": "add"}]}},
        {"type": "thread.started"},
    ]
    shown = [e for event in codex for e in log_routes.normalize(event)]
    assert [e["kind"] for e in shown] == ["message", "command", "error", "files"]
    assert shown[2]["exit_code"] == 1 and shown[3]["text"] == "add a.py"
    claude = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking"},
                                                           {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}
    assert [e["text"] for e in log_routes.normalize(claude)] == ["Looking", "Bash: ls"]
    result = log_routes.normalize({"type": "result", "result": "VERDICT: PASS", "total_cost_usd": 0.0123})
    assert result == [{"kind": "result", "text": "VERDICT: PASS · $0.012"}]


def test_paths_must_be_under_the_log_mounts(tmp_path, monkeypatch):
    monkeypatch.setattr(log_routes, "MOUNTS", ((log_routes.Path("/var/log/sid-ai/jobs"), tmp_path),))
    assert log_routes.container_path("/var/log/sid-ai/jobs/abc.jsonl") == (tmp_path / "abc.jsonl").resolve()
    assert log_routes.container_path("/var/log/sid-ai/jobs/../../../etc/passwd") is None
    assert log_routes.container_path("/etc/sid-ai/operator.env") is None
    assert log_routes.container_path("/var/log/sid-ai/jobs/x.env") is None


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(log_routes, "MOUNTS", ((log_routes.Path("/var/log/sid-ai/jobs"), tmp_path),))
    fake = FakeRedis({"sid:jobs:j1": {"id": "j1", "status": "running", "log": "/var/log/sid-ai/jobs/j1.jsonl"}})
    monkeypatch.setattr(main, "redis", fake)
    with TestClient(main.app) as client:
        yield client, tmp_path, fake


def test_incremental_reads_only_consume_whole_lines(logs):
    client, folder, fake = logs
    log = folder / "j1.jsonl"
    first = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "one"}})
    log.write_text(first + "\n" + '{"type": "item.compl')  # second line still being written
    page = client.get("/api/jobs/j1/log").json()
    assert [e["text"] for e in page["events"]] == ["one"] and page["running"] and page["next"] == len(first) + 1
    with log.open("a") as handle:
        handle.write('eted", "item": {"type": "agent_message", "text": "two"}}\n')
    page = client.get(f"/api/jobs/j1/log?after={page['next']}").json()
    assert [e["text"] for e in page["events"]] == ["two"]
    (folder / "j1-tests.log").write_text("1 passed\n")
    assert client.get("/api/jobs/j1/log?tests=true").json()["events"][0]["text"] == "1 passed\n"
    assert client.get("/api/jobs/nope/log").status_code == 404
    fake.hashes["sid:jobs:j2"] = {"id": "j2", "status": "merged", "log": "/elsewhere/j2.jsonl"}
    assert client.get("/api/jobs/j2/log").json()["available"] is False


def test_specialist_review_parts(logs):
    client, folder, fake = logs
    (folder / "j1.jsonl").write_text("")
    (folder / "j1.spec.json").write_text(json.dumps({"type": "result", "result": "VERDICT: PASS"}) + "\n")
    (folder / "j1.safety.json").write_text("")
    main_log = client.get("/api/jobs/j1/log").json()
    assert main_log["parts"] == ["safety", "spec"] and main_log["events"] == []
    spec = client.get("/api/jobs/j1/log?part=spec").json()
    assert spec["events"][0]["text"] == "VERDICT: PASS"
    assert client.get("/api/jobs/j1/log?part=missing").status_code == 404
    assert client.get("/api/jobs/j1/log?part=../x").status_code == 422
