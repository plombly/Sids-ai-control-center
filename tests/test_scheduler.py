"""Importance scheduler: tiers, least remaining effort, in-flight first, aging."""

import json

import pytest

from sid_testing import MemoryRedis, ROOT, load_module

NOW = 1_800_000_000.0
QUEUE = "sid:jobs"


class SchedRedis(MemoryRedis):
    def smembers(self, key):
        return set(self.values.get(key, set()))


@pytest.fixture
def worker():
    module = load_module(ROOT / "services/worker/worker.py")
    module.redis = SchedRedis()
    module.WORKER_CLASS = "general"
    return module


def project(r, pid, importance, remaining):
    r.records[f"sid:projects:{pid}"] = {"id": pid, "name": pid, "importance": importance, "repo": f"/p/{pid}",
                                        "worktrees": f"/p/{pid}/w", "logs": f"/p/{pid}/l", "status": "active"}
    r.values.setdefault("sid:projects", set()).add(pid)
    r.records[f"sid:project-stats:{pid}"] = {"remaining_effort": str(remaining)}


def enqueue(r, job_id, pid, role="builder", age_min=0):
    payload = {"id": job_id, "role": role, "project_id": pid, "created_at": NOW - age_min * 60}
    r.rpush(QUEUE, json.dumps(payload))


def order(worker, n=None, now=NOW):
    picked = []
    while True:
        raw = worker.pick_job(now=now)
        if raw is None:
            return picked
        picked.append(json.loads(raw)["id"])
        if n and len(picked) == n:
            return picked


def test_general_workers_take_high_then_medium_then_low(worker):
    r = worker.redis
    project(r, "lo", "low", 1)
    project(r, "med", "medium", 1)
    project(r, "hi", "high", 50)
    for pid in ("lo", "med", "hi"):
        enqueue(r, f"{pid}-1", pid)
    assert order(worker) == ["hi-1", "med-1", "lo-1"]


def test_support_workers_cover_medium_and_low_before_high(worker):
    r = worker.redis
    project(r, "lo", "low", 1)
    project(r, "med", "medium", 1)
    project(r, "hi", "high", 1)
    for pid in ("hi", "lo", "med"):
        enqueue(r, f"{pid}-1", pid)
    worker.WORKER_CLASS = "support"
    assert order(worker) == ["med-1", "lo-1", "hi-1"]


def test_support_worker_takes_high_work_when_nothing_else_waits(worker):
    project(worker.redis, "hi", "high", 1)
    enqueue(worker.redis, "hi-1", "hi")
    worker.WORKER_CLASS = "support"
    assert order(worker) == ["hi-1"]


def test_least_remaining_effort_wins_within_a_tier(worker):
    r = worker.redis
    project(r, "big", "high", 600)
    project(r, "small", "high", 6)
    enqueue(r, "big-1", "big", age_min=5)
    enqueue(r, "small-1", "small")
    assert order(worker, 1) == ["small-1"]


def test_dumping_500_prompts_switches_priority_immediately(worker):
    r = worker.redis
    project(r, "a", "high", 5)
    project(r, "b", "high", 20)
    for i in range(3):
        enqueue(r, f"a-{i}", "a")
        enqueue(r, f"b-{i}", "b")
    assert order(worker, 1) == ["a-0"]
    r.records["sid:project-stats:a"]["remaining_effort"] = str(5 + 500 * 3)  # someone dumps 500 prompts
    assert order(worker, 1) == ["b-0"]


def test_in_flight_work_goes_before_new_builds(worker):
    r = worker.redis
    project(r, "p", "high", 10)
    enqueue(r, "build", "p", age_min=10)
    enqueue(r, "review", "p", role="reviewer")
    enqueue(r, "repair", "p", role="repair")
    assert order(worker)[:2] == ["review", "repair"]


def test_aging_keeps_a_big_project_from_starving(worker):
    r = worker.redis
    project(r, "big", "high", 60)
    project(r, "small", "high", 3)
    enqueue(r, "small-new", "small")
    enqueue(r, "big-old", "big", age_min=200)  # waited long: 60 - 0.5*200 < 3
    assert order(worker, 1) == ["big-old"]


def test_importance_change_takes_effect_on_the_next_pick(worker):
    r = worker.redis
    project(r, "a", "medium", 1)
    project(r, "b", "low", 1)
    enqueue(r, "a-1", "a")
    enqueue(r, "b-1", "b")
    r.records["sid:projects:b"]["importance"] = "high"
    assert order(worker, 1) == ["b-1"]


def test_lost_claim_race_moves_to_the_next_job(worker, monkeypatch):
    r = worker.redis
    project(r, "p", "high", 1)
    enqueue(r, "first", "p", age_min=2)
    enqueue(r, "second", "p")
    real_lrem = r.lrem

    def contended(key, count, value):
        if json.loads(value)["id"] == "first":
            real_lrem(key, count, value)  # another worker took it
            return 0
        return real_lrem(key, count, value)

    monkeypatch.setattr(r, "lrem", contended)
    assert json.loads(worker.pick_job(now=NOW))["id"] == "second"


def test_jobs_without_project_run_as_sid_and_bad_payloads_do_not_break_picking(worker):
    r = worker.redis
    r.rpush(QUEUE, json.dumps({"id": "legacy", "role": "builder", "created_at": NOW}))
    r.rpush(QUEUE, "not json")
    picked = {worker.pick_job(now=NOW), worker.pick_job(now=NOW)}
    assert picked == {json.dumps({"id": "legacy", "role": "builder", "created_at": NOW}), "not json"}
    assert worker.pick_job(now=NOW) is None


def test_heartbeat_reports_worker_class(worker):
    worker.WORKER_CLASS = "support"
    worker.heartbeat()
    assert worker.redis.records[worker.worker_key()]["worker_class"] == "support"


# --- orchestrator: remaining effort ---------------------------------------------------

@pytest.fixture
def orch():
    module = load_module(ROOT / "services/orchestrator/orchestrator.py")
    module.r = SchedRedis()
    return module


def test_project_stats_count_remaining_effort_by_size(orch):
    r = orch.r
    jobs = {
        "a": {"status": "queued", "size": "S", "project_id": "web"},
        "b": {"status": "running", "size": "L", "project_id": "web"},
        "c": {"status": "awaiting_review", "size": "M", "project_id": "web"},
        "d": {"status": "merged", "size": "L", "project_id": "web"},
        "e": {"status": "needs_human", "size": "L", "project_id": "web"},
        "f": {"status": "blocked", "project_id": "web"},  # no size: M
        "g": {"status": "test_failed", "build_attempt": "1", "size": "S", "project_id": "web"},  # retry pending
        "h": {"status": "queued", "size": "S"},  # sid
        "rv": {"status": "queued", "role": "reviewer", "project_id": "web"},  # not a build
    }
    for jid, fields in jobs.items():
        r.records[f"sid:jobs:{jid}"] = {"id": jid, "role": fields.pop("role", "builder"), **fields}
    r.records["sid:goals:g1"] = {"id": "g1", "status": "queued", "project_id": "web"}
    r.records["sid:goals:g2"] = {"id": "g2", "status": "running", "project_id": "web", "jobs": '["a"]'}
    stats = orch.publish_project_stats(NOW)
    assert stats["web"] == {"remaining_effort": 1 + 8 + 3 + 3 + 1 + 3, "waiting_jobs": 2, "running_jobs": 1}
    assert stats["sid"]["remaining_effort"] == 1
    assert r.records["sid:project-stats:web"]["remaining_effort"] == "19"


def test_planner_size_is_stored_and_bad_sizes_become_medium(orch):
    plan = {"jobs": [
        {"number": 1, "title": "a", "task": "t", "scope": [], "size": "L", "depends_on": []},
        {"number": 2, "title": "b", "task": "t", "scope": [], "size": "huge", "depends_on": []},
    ]}
    jobs = orch.validate_plan(plan)
    assert [j["size"] for j in jobs] == ["L", "M"]
    assert '"size": "S|M|L"' in orch.planner_prompt("g")


def test_long_scopes_are_compressed_to_top_level_entries(orch):
    files = [f"lib/screens/s{i}.dart" for i in range(10)] + ["test/a_test.dart", "pubspec.yaml", "README.md",
                                                             "android/app/build.gradle", "ios/Runner/Info.plist"]
    plan = {"jobs": [{"number": 1, "title": "t", "task": "x", "scope": files}]}
    jobs = orch.validate_plan(plan)
    assert jobs[0]["scope"] == ["lib/", "test/", "pubspec.yaml", "README.md", "android/", "ios/"]
    wide = {"jobs": [{"number": 1, "title": "t", "task": "x", "scope": [f"d{i}/f" for i in range(13)]}]}
    with pytest.raises(ValueError, match="too broad"):
        orch.validate_plan(wide)


def test_builders_get_the_goal_verbatim(orch):
    item = {"task": "Add the servers screen with the exact keys", "scope": []}
    goal = 'Pairing code contract: {"v":1,"name":"Home SID","url":"http://x","key":"sidk_y"}'
    prompt = orch.scoped_builder_prompt(item, goal=goal)
    assert prompt.startswith("Task:\nAdd the servers screen")
    assert '<goal>\nPairing code contract: {"v":1,"name":"Home SID"' in prompt and "This job is the whole goal." in prompt
    assert "do only this job's part" in orch.scoped_builder_prompt(item, goal=goal, jobs_in_plan=3)
    assert "<goal>" not in orch.scoped_builder_prompt(item)
    long = orch.scoped_builder_prompt(item, goal="x" * 20000)
    assert "(goal shortened)" in long and len(long) < 16000
