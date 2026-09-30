"""Scope-aware scheduling: jobs that change the same files never run at once."""

import json

import pytest

from sid_testing import MemoryRedis, ROOT, load_module


@pytest.fixture
def orch():
    module = load_module(ROOT / "services/orchestrator/orchestrator.py")
    module.r = MemoryRedis()
    return module


def put(orch, job_id, status, scope, created="1", deps=(), **fields):
    orch.r.records[f"sid:jobs:{job_id}"] = {
        "id": job_id, "role": "builder", "status": status, "prompt": f"do {job_id}",
        "scope": json.dumps(list(scope)), "dependencies": json.dumps(list(deps)),
        "created_at": created, **fields}
    return orch.r.records[f"sid:jobs:{job_id}"]


def job(orch, job_id):
    return orch.r.records[f"sid:jobs:{job_id}"]


def queued(orch):
    return [json.loads(raw)["id"] for raw in orch.r.values.get("sid:jobs", [])]


@pytest.mark.parametrize("a,b,overlap", [
    ("apps/web/app.js", "apps/web/app.js", True),
    ("apps/web", "apps/web/app.js", True),
    ("apps/web/", "apps/web/lib/api.js", True),
    ("apps/web/app.js", "apps/web/app.test.js", False),
    ("apps/api", "apps/api-v2/x.py", False),
    ("./apps/api/main.py", "apps/api/main.py", True),
])
def test_path_overlap(orch, a, b, overlap):
    x = put(orch, "x", "queued", [a])
    y = put(orch, "y", "running", [b])
    assert bool(orch.scope_conflict(x)) is overlap
    assert bool(orch.scope_conflict(y)) is overlap


@pytest.mark.parametrize("status,holds", [
    ("queued", True), ("claimed", True), ("running", True), ("testing", True),
    ("awaiting_review", True), ("needs_human", True),
    ("merged", False), ("rejected", False), ("completed_no_changes", False),
    ("blocked", False), ("blocked_failed_dependency", False),
])
def test_which_states_hold_files(orch, status, holds):
    put(orch, "a", status, ["apps/api/main.py"])
    b = put(orch, "b", "blocked", ["apps/api/main.py"])
    assert bool(orch.scope_conflict(b)) is holds


def test_failed_build_awaiting_retry_keeps_its_files(orch):
    put(orch, "a", "test_failed", ["apps/api/main.py"], build_attempt="1")
    assert orch.scope_conflict(put(orch, "b", "blocked", ["apps/api/main.py"]))
    job(orch, "a")["build_attempt"] = "2"  # exhausted: final, releases
    assert orch.scope_conflict(job(orch, "b")) is None


def test_no_scope_claims_nothing(orch):
    put(orch, "a", "running", [])
    assert orch.scope_conflict(put(orch, "b", "blocked", ["apps/api/main.py"])) is None
    put(orch, "c", "running", ["apps/api/main.py"])
    assert orch.scope_conflict(put(orch, "d", "blocked", [])) is None


def test_reason_names_files_and_holder(orch):
    put(orch, "a", "running", ["apps/web/app.js", "apps/web/index.html"])
    holder, reason = orch.scope_conflict(put(orch, "b", "blocked", ["apps/web/index.html"]))
    assert holder == "a"
    assert reason == "waiting for apps/web/index.html held by job a (running)"


def test_waiting_job_is_released_when_files_free_up(orch):
    put(orch, "a", "running", ["apps/api/main.py"])
    put(orch, "b", "blocked", ["apps/api/main.py"], created="2")
    orch.release_dependencies()
    assert job(orch, "b")["status"] == "blocked"
    assert "held by job a" in job(orch, "b")["blocked_reason"]
    assert queued(orch) == []
    job(orch, "a")["status"] = "merged"
    orch.release_dependencies()
    assert job(orch, "b")["status"] == "queued"
    assert job(orch, "b")["blocked_reason"] == ""
    assert queued(orch) == ["b"]


def test_release_is_fifo_and_one_at_a_time_per_file(orch):
    put(orch, "late", "blocked", ["apps/api/main.py"], created="3")
    put(orch, "early", "blocked", ["apps/api/main.py"], created="2")
    put(orch, "other", "blocked", ["apps/web/app.js"], created="4")
    orch.release_dependencies()
    assert sorted(queued(orch)) == ["early", "other"], "disjoint work runs in parallel"
    assert job(orch, "late")["status"] == "blocked"
    assert "held by job early" in job(orch, "late")["blocked_reason"]


def test_dependencies_still_come_first(orch):
    put(orch, "dep", "running", ["apps/other.py"])
    put(orch, "b", "blocked", ["apps/api/main.py"], created="2", deps=["dep"])
    orch.release_dependencies()
    assert job(orch, "b")["status"] == "blocked"
    job(orch, "dep")["status"] = "merged"
    orch.release_dependencies()
    assert queued(orch) == ["b"]


def test_planned_jobs_wait_for_busy_files_and_are_retry_eligible(orch, monkeypatch):
    put(orch, "busy", "awaiting_review", ["apps/web/app.js"])
    orch.r.records["sid:goals:g1"] = {"id": "g1", "goal": "ui work", "status": "queued"}
    monkeypatch.setattr(orch, "run_planner", lambda goal, atomic=False, info=None, project=None: {"jobs": [
        {"number": 1, "title": "ui", "task": "t1", "scope": ["apps/web/app.js"], "depends_on": []},
        {"number": 2, "title": "api", "task": "t2", "scope": ["apps/api/main.py"], "depends_on": []},
    ]})
    monkeypatch.setattr(orch, "repository_manifest", lambda repo=None: "")
    orch.process_goal(json.dumps({"id": "g1", "goal": "ui work"}))
    jobs = {j["title"]: j for k, j in orch.r.records.items() if k.startswith("sid:jobs:") and j.get("title")}
    assert jobs["ui"]["status"] == "blocked"
    assert "held by job busy" in jobs["ui"]["blocked_reason"]
    assert jobs["api"]["status"] == "queued"
    assert queued(orch) == [jobs["api"]["id"]]
    assert jobs["ui"]["build_attempt"] == jobs["api"]["build_attempt"] == "1"


def test_planner_is_told_which_files_are_busy(orch, monkeypatch):
    monkeypatch.setattr(orch, "repository_manifest", lambda repo=None: "")
    put(orch, "busy", "running", ["apps/api/main.py"])
    put(orch, "done", "merged", ["apps/web/app.js"])
    prompt = orch.planner_prompt("goal")
    assert "  - apps/api/main.py" in prompt
    assert "apps/web/app.js\n" not in prompt.split("in-flight jobs")[1]
    assert "registerPanel" in prompt
