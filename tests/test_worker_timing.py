import json

import pytest

from laika_testing import MemoryRedis, ROOT, load_module


@pytest.fixture
def worker_module():
    module = load_module(ROOT / "services/worker/worker.py")
    module.redis = MemoryRedis()
    module.CURRENT_JOB_ID = ""
    module.CURRENT_JOB_ROLE = ""
    module.CURRENT_JOB_STARTED_AT = ""
    return module


def raw_job(job_id="j1", role="builder"):
    return json.dumps({"id": job_id, "role": role})


def test_claim_sets_started_and_removes_finished(worker_module, monkeypatch):
    key = "laika:jobs:j1"
    worker_module.redis.records[key] = {"finished_at": "old", "status": "queued"}
    monkeypatch.setattr(worker_module.time, "time", lambda: 100.5)
    seen = {}
    worker_module._process_job = lambda raw: seen.update(worker_module.redis.records[key])

    worker_module.process_job(raw_job())

    job = worker_module.redis.records[key]
    assert float(job["started_at"]) == 100.5
    assert "finished_at" not in seen


def test_reclaim_overwrites_started_at(worker_module, monkeypatch):
    key = "laika:jobs:j1"
    worker_module.redis.records[key] = {}
    times = iter((10.0, 10.0, 10.0, 10.0, 20.0, 20.0, 20.0, 20.0))
    monkeypatch.setattr(worker_module.time, "time", lambda: next(times))
    worker_module._process_job = lambda raw: None

    worker_module.process_job(raw_job())
    first = worker_module.redis.records[key]["started_at"]
    worker_module.process_job(raw_job())

    assert first == "10.0"
    assert worker_module.redis.records[key]["started_at"] == "20.0"


@pytest.mark.parametrize("handler", [
    lambda module, key: None,
    lambda module, key: module.redis.hset(key, "status", "failed"),
])
def test_finished_at_written_after_success_and_failure(worker_module, monkeypatch, handler):
    key = "laika:jobs:j1"
    worker_module.redis.records[key] = {}
    clock = iter((1.0, 1.0, 2.0, 2.0))
    monkeypatch.setattr(worker_module.time, "time", lambda: next(clock))
    worker_module._process_job = lambda raw: handler(worker_module, key)

    worker_module.process_job(raw_job())

    assert worker_module.redis.records[key]["finished_at"] == "2.0"


def test_finished_at_written_when_handler_raises(worker_module, monkeypatch):
    key = "laika:jobs:j1"
    worker_module.redis.records[key] = {}
    clock = iter((3.0, 3.0, 4.0, 4.0))
    monkeypatch.setattr(worker_module.time, "time", lambda: next(clock))

    def fail(raw):
        raise RuntimeError("boom")

    worker_module._process_job = fail
    with pytest.raises(RuntimeError, match="boom"):
        worker_module.process_job(raw_job())

    assert worker_module.redis.records[key]["finished_at"] == "4.0"


def test_heartbeat_publishes_started_at_while_busy_and_clears_when_idle(worker_module):
    key = "laika:jobs:j1"
    worker_module.redis.records[key] = {}
    seen = {}

    def handler(raw):
        seen.update(worker_module.redis.records[worker_module.worker_key()])

    worker_module._process_job = handler
    worker_module.process_job(raw_job())

    assert seen["job_id"] == "j1"
    assert seen["job_started_at"] == worker_module.redis.records[key]["started_at"]
    idle = worker_module.redis.records[worker_module.worker_key()]
    assert idle["job_id"] == ""
    assert idle["job_started_at"] == ""


def test_deleted_job_hash_is_not_recreated(worker_module):
    worker_module._process_job = lambda raw: None

    worker_module.process_job(raw_job("gone"))

    assert "laika:jobs:gone" not in worker_module.redis.records
