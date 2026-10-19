"""Job status history (services/job_events.py)."""

from laika_testing import MemoryRedis, ROOT, load_module

events = load_module(ROOT / "services/job_events.py", "job_events_test")


def test_status_changes_are_recorded_with_actor_and_reason():
    store = MemoryRedis({"laika:jobs:b1": {"id": "b1", "status": "running"}})
    logged = []
    client = events.JobEventRedis(store, "orchestrator", log=logged.append)
    client.hset("laika:jobs:b1", mapping={"status": "failed", "error": "worker lost while running"})
    client.hset("laika:jobs:b1", mapping={"status": "failed", "updated_at": "1"})     # no change: not recorded
    client.hset("laika:jobs:b1", "status", "queued")
    client.hset("laika:goals:g1", mapping={"status": "running"})                       # not a job
    timeline = events.timeline(store, "b1")
    assert [(e["from"], e["to"], e["actor"]) for e in timeline] == [("running", "failed", "orchestrator"),
                                                                    ("failed", "queued", "orchestrator")]
    assert timeline[0]["reason"] == "worker lost while running" and not timeline[0]["unexpected"]
    assert logged == []


def test_unexpected_transitions_are_recorded_and_logged_not_refused():
    store = MemoryRedis({"laika:jobs:b2": {"id": "b2", "status": "merged"}})
    logged = []
    client = events.JobEventRedis(store, "worker w1", log=logged.append)
    client.hset("laika:jobs:b2", mapping={"status": "running"})
    assert store.records["laika:jobs:b2"]["status"] == "running"                        # warn-only
    assert events.timeline(store, "b2")[0]["unexpected"] is True
    assert "unexpected transition for job b2: merged -> running by worker w1" in logged[0]


def test_the_workers_fenced_writes_are_recorded_too():
    worker = load_module(ROOT / "services/worker/worker.py", "worker_events_test")
    store = MemoryRedis({"laika:jobs:b3": {"id": "b3", "status": "running", "execution_id": "run"}})
    fenced = worker.FencedRedis(store, fences={"laika:jobs:b3": "run"})
    fenced.hset("laika:jobs:b3", mapping={"status": "testing"})
    assert [(e["from"], e["to"]) for e in events.timeline(store, "b3")] == [("running", "testing")]


def test_reviews_repairs_and_integrations_start_straight_from_queued():
    for working in ("reviewing", "repairing", "integrating"):
        assert events.allowed("queued", working)
    assert events.allowed("reviewing", "review_complete") and not events.allowed("merged", "queued")
