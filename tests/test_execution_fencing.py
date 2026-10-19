"""A worker's late writes cannot land on a job that was re-run elsewhere."""

import pytest

from laika_testing import MemoryRedis, ROOT, load_module


@pytest.fixture
def worker():
    module = load_module(ROOT / "services/worker/worker.py", "worker_fencing_test")
    module.FENCES.clear()
    return module


def test_writes_land_only_while_the_run_still_owns_the_job(worker):
    store = MemoryRedis({"laika:jobs:b1": {"id": "b1", "status": "running", "execution_id": "run-1"}})
    fenced = worker.FencedRedis(store, fences={"laika:jobs:b1": "run-1"})
    fenced.hset("laika:jobs:b1", mapping={"status": "testing"})
    assert store.records["laika:jobs:b1"]["status"] == "testing"
    store.records["laika:jobs:b1"]["execution_id"] = "revoked:abc"       # the orchestrator took it away
    with pytest.raises(worker.StaleExecution):
        fenced.hset("laika:jobs:b1", mapping={"status": "awaiting_review", "candidate_commit": "c" * 40})
    with pytest.raises(worker.StaleExecution):
        fenced.hdel("laika:jobs:b1", "error")
    assert store.records["laika:jobs:b1"]["status"] == "testing" and "candidate_commit" not in store.records["laika:jobs:b1"]
    fenced.hset("laika:jobs:other", mapping={"x": "1"})                    # other keys are never fenced
    assert store.records["laika:jobs:other"]["x"] == "1"


def test_a_lost_job_is_revoked_before_it_is_handed_on():
    orch = load_module(ROOT / "services/orchestrator/orchestrator.py", "orch_fencing_test")
    orch.r = MemoryRedis({
        "laika:jobs:b1": {"id": "b1", "role": "builder", "status": "running", "worker_id": "w1",
                          "build_attempt": "1", "execution_id": "run-1"},
        "laika:workers:w1": {"status": "idle", "job_id": ""},
    })
    for t in (1000.0, 1000.0 + orch.LOST_CONFIRM_SECONDS):
        orch.recover_lost_jobs(t)
    job = orch.r.records["laika:jobs:b1"]
    assert job["execution_id"].startswith("revoked:") and job["status"] == "deferred"


def test_each_run_gets_a_new_identity_and_a_late_run_stops_quietly(worker, monkeypatch):
    store = MemoryRedis({"laika:jobs:b1": {"id": "b1", "status": "queued"}})
    worker.redis = worker.FencedRedis(store)
    seen = []

    def run(raw):
        seen.append(store.records["laika:jobs:b1"]["execution_id"])
        store.records["laika:jobs:b1"]["execution_id"] = "revoked:x"       # taken away mid-run
        worker.redis.hset("laika:jobs:b1", mapping={"status": "awaiting_review"})
    monkeypatch.setattr(worker, "_process_job", run)
    monkeypatch.setattr(worker, "heartbeat", lambda *a, **k: None)
    worker.process_job('{"id": "b1"}')                                       # no exception escapes
    assert seen[0] and store.records["laika:jobs:b1"]["status"] == "queued"
    assert store.records["laika:jobs:b1"]["executions"] == "1" and worker.FENCES == {}
