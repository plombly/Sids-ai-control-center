"""Worker scaler: when it adds, removes, drains and holds workers, and how
it starts and stops units (services/scaler/laika_scaler.py)."""

import json
import subprocess

import pytest

from laika_testing import ROOT, MemoryRedis, load_module


@pytest.fixture
def sc():
    return load_module(ROOT / "services/scaler/laika_scaler.py", "laika_scaler_test")


def cfg(sc, **env):
    base = {"AUTOSCALE": "true", "MIN_WORKERS": "1", "MAX_WORKERS": "6"}
    return sc.Config({**base, **env}, 4, 7.1)


def state(**fields):
    base = {"target": 3, "idle": 0, "runnable": 0, "projects": 0, "memory_percent": 60.0,
            "memory_free_gb": 4.0, "load5": 0.5, "cpus": 4, "manual_until": 0, "hold": False}
    return {**base, **fields}


def test_suggested_maximum_follows_the_hardware(sc):
    assert sc.Config({}, 4, 7.1).max == 7
    assert sc.Config({}, 1, 1.0).max == 2
    assert sc.Config({}, 64, 512).max == 16
    assert sc.Config({"MAX_WORKERS": "3", "MIN_WORKERS": "5"}, 4, 7.1).min == 3


def test_ready_work_counts_only_what_could_start_now(sc):
    jobs = [json.dumps({"role": "builder", "project_id": "a"}), json.dumps({"role": "builder", "project_id": "b"}),
            json.dumps({"role": "reviewer", "project_id": "a"}), json.dumps({"role": "reviewer", "project_id": "c"}),
            json.dumps({"role": "repair", "project_id": "c"}), "not json"]
    provider = {"builder": "codex", "reviewer": "claude", "repair": "claude"}.get
    assert sc.ready_work(jobs, provider, free_claude=1, claude_cooling=False) == (3, 3)
    assert sc.ready_work(jobs, provider, free_claude=0, claude_cooling=False) == (2, 3)
    # Claude cooling down: those jobs fall back to Codex and can all run.
    assert sc.ready_work(jobs, provider, free_claude=0, claude_cooling=True) == (5, 3)


def test_adds_only_after_parallel_work_waited(sc):
    c, memo = cfg(sc), {}
    s = state(runnable=2, idle=0, projects=2)
    assert sc.decide(c, s, memo, 1000)[0] == 3            # just started waiting
    assert sc.decide(c, s, memo, 1000 + 179)[0] == 3
    target, hold, reason = sc.decide(c, s, memo, 1000 + 180)
    assert target == 4 and "2 jobs ready to run in parallel (2 projects)" in reason
    assert sc.decide(c, state(target=4, runnable=2, projects=2), memo, 1000 + 200)[0] == 4  # cooldown
    assert sc.decide(c, state(target=4, runnable=2, projects=2), memo, 1000 + 241)[0] == 5


def test_no_add_when_idle_workers_can_take_the_work_or_at_the_maximum(sc):
    c, memo = cfg(sc), {}
    for now in (0, 400):
        assert sc.decide(c, state(runnable=2, idle=2), memo, now)[0] == 3
    memo = {}
    for now in (0, 400):
        assert sc.decide(c, state(target=6, runnable=5), memo, now)[0] == 6


def test_no_add_without_room(sc):
    c, memo = cfg(sc), {}
    for now in (0, 400):
        assert sc.decide(c, state(runnable=3, memory_percent=20, memory_free_gb=1.4), memo, now)[0] == 3
    memo = {}
    for now in (0, 400):
        assert sc.decide(c, state(runnable=3, load5=4.5), memo, now)[0] == 3


def test_removes_idle_workers_slowly_down_to_the_minimum(sc):
    c, memo = cfg(sc, MIN_WORKERS="2"), {}
    s = state(target=4, idle=3)
    assert sc.decide(c, s, memo, 0)[0] == 4
    target, _, reason = sc.decide(c, s, memo, 900)
    assert target == 3 and "idle for 15 min" in reason
    assert sc.decide(c, state(target=3, idle=2), memo, 1000)[0] == 3
    assert sc.decide(c, state(target=3, idle=2), memo, 1200)[0] == 2
    assert sc.decide(c, state(target=2, idle=2), memo, 99999)[0] == 2


def test_memory_pressure_drains_and_critical_memory_holds(sc):
    c, memo = cfg(sc), {}
    assert sc.decide(c, state(memory_percent=12), memo, 0)[:2] == (3, False)
    target, hold, reason = sc.decide(c, state(memory_percent=12), memo, 30)
    assert target == 2 and not hold and "memory low (12% free)" in reason
    assert sc.decide(c, state(target=2, memory_percent=12), memo, 60)[0] == 2   # one at a time
    assert sc.decide(c, state(target=2, memory_percent=12), memo, 150)[0] == 1
    assert sc.decide(c, state(target=1, memory_percent=12), memo, 400)[0] == 1  # never 0
    assert sc.decide(c, state(target=1, memory_percent=5), memo, 401)[1] is True
    assert sc.decide(c, state(target=1, memory_percent=10, hold=True), memo, 402)[1] is True   # still low
    assert sc.decide(c, state(target=1, memory_percent=40, hold=True), memo, 403)[1] is False
    # Calm period: the work that waits does not bring the worker straight back.
    for now in (404, 700):
        assert sc.decide(c, state(target=1, runnable=3), memo, now)[0] == 1
    assert sc.decide(c, state(target=1, runnable=3), memo, 1001)[0] == 2


def test_load_pressure_needs_two_minutes(sc):
    c, memo = cfg(sc), {}
    assert sc.decide(c, state(load5=6.5), memo, 0)[0] == 3
    assert sc.decide(c, state(load5=6.5), memo, 119)[0] == 3
    target, _, reason = sc.decide(c, state(load5=6.5), memo, 120)
    assert target == 2 and "load high (6.5 on 4 CPUs)" in reason


def test_fixed_mode_and_manual_changes(sc):
    c = cfg(sc, AUTOSCALE="false", WORKER_COUNT="5")
    assert sc.decide(c, state(), {}, 0)[:1] == (5,)
    assert sc.decide(c, state(target=5, idle=5), {}, 99999)[0] == 5   # never idles down
    auto = cfg(sc)
    assert sc.decide(auto, state(target=5, idle=5, manual_until=500), {"idle_since": 0}, 400)[0] == 5
    assert sc.decide(auto, state(target=9), {}, 0)[0] == 6


def test_converge_starts_and_drains_without_killing_jobs(sc):
    r, calls = MemoryRedis(), []
    runner = lambda *argv: calls.append(argv)
    r.records["laika:workers:laika-worker-04"] = {"status": "working", "job_id": "abc"}
    r.records["laika:workers:laika-worker-05"] = {"status": "draining", "job_id": ""}
    r.values["laika:worker-drain:laika-worker-02"] = "1"
    sc.converge(r, 3, {1, 2, 4, 5}, runner, log=lambda m: None)
    assert ("systemctl", "enable", "--now", "laika-worker@03.service") in calls
    assert ("systemctl", "disable", "--now", "laika-worker@05.service") in calls
    assert not any(c[-1] == "laika-worker@04.service" for c in calls)    # still on its job
    assert r.get("laika:worker-drain:laika-worker-04") == "1"
    assert r.get("laika:worker-drain:laika-worker-02") is None             # back in service
    # An idle worker that has not acknowledged the drain yet keeps running.
    r.records["laika:workers:laika-worker-04"] = {"status": "idle", "job_id": ""}
    calls.clear()
    sc.converge(r, 3, {1, 2, 3, 4}, runner, log=lambda m: None)
    assert calls == []


def test_step_publishes_state_logs_changes_and_holds(sc, monkeypatch):
    r = MemoryRedis()
    monkeypatch.setattr(sc, "stored_env", lambda r: {"AUTOSCALE": "true", "MAX_WORKERS": "6"})
    calls = []
    target, reason = sc.step(r, {}, now=1000, cpus=4, memory=(60.0, 4.0, 7.1), load=0.3,
                             runner=lambda *a: calls.append(a), units={1, 2, 3})
    assert target == 3 and r.get("laika:scaler:target") == 3 or r.get("laika:scaler:target") == "3"
    published = json.loads(r.get("laika:scaler:state"))
    assert published["target"] == 3 and published["max"] == 6 and published["auto"] is True
    sc.step(r, {}, now=1015, cpus=4, memory=(5.0, 0.3, 7.1), load=0.3, runner=lambda *a: None, units={1, 2, 3})
    assert r.get("laika:scaler:hold")
    log = [json.loads(x) for x in r.lrange("laika:scaler:log", 0, -1)]
    assert any("holding new work" in e["reason"] for e in log)


def test_worker_pauses_for_drain_and_hold(monkeypatch):
    worker = load_module(ROOT / "services/worker/worker.py", "worker_scaler_test")
    r = MemoryRedis()
    monkeypatch.setattr(worker, "redis", r)
    monkeypatch.setattr(worker, "WORKER_ID", "laika-worker-05")
    assert worker.pause_reason() is None
    r.values["laika:scaler:hold"] = "memory critical"
    assert worker.pause_reason() == "held"
    r.values["laika:worker-drain:laika-worker-05"] = "1"
    assert worker.pause_reason() == "draining"
    r.values["laika:worker-control:laika-worker-05"] = "disabled"
    assert worker.pause_reason() == "disabled"
    # Support workers are the last of the running ones, one in four.
    assert [worker.worker_class(f"laika-worker-{n:02d}", {"SUPPORT_WORKERS": "2"}, running=5) for n in (4, 5, 6)] == \
        ["general", "support", "general"]
    assert worker.worker_class("laika-worker-03", {"SUPPORT_WORKERS": "2"}, running=3) == "general"


def test_stored_worker_settings_reach_the_scaler(sc):
    import settings_schema
    r = MemoryRedis()
    settings_schema.save(r, settings_schema.validate({"AUTOSCALE": "false", "MAX_WORKERS": 5, "THEME": "light"})[0])
    env = sc.stored_env(r)
    assert env["AUTOSCALE"] == "false" and env["MAX_WORKERS"] == "5" and "THEME" not in env
