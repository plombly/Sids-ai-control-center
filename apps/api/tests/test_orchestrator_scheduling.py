import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "sid_orchestrator", ROOT / "services/orchestrator/orchestrator.py"
)
orchestrator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(orchestrator)


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.queues = {}
        self.reservations = set()

    def hset(self, key, key_or_mapping=None, value=None, mapping=None):
        fields = mapping or ({key_or_mapping: value} if key_or_mapping else {})
        self.hashes.setdefault(key, {}).update(fields)

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hsetnx(self, key, field, value):
        record = self.hashes.setdefault(key, {})
        if field in record:
            return False
        record[field] = value
        return True

    def hdel(self, key, *fields):
        for field in fields:
            self.hashes.get(key, {}).pop(field, None)

    def rpush(self, key, value):
        self.queues.setdefault(key, []).append(value)

    def lpop(self, key):
        values = self.queues.get(key, [])
        return values.pop(0) if values else None

    def scan_iter(self, pattern):
        prefix = pattern.removesuffix("*")
        return (key for key in list(self.hashes) if key.startswith(prefix))

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.hashes:
            return False
        self.hashes[key] = {"value": value}
        return True

    def delete(self, key):
        self.hashes.pop(key, None)


def install_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(orchestrator, "r", fake)
    return fake


def test_two_goals_are_submitted_without_waiting(monkeypatch):
    fake = install_redis(monkeypatch)
    fake.queues[orchestrator.GOAL_QUEUE] = ["goal-1", "goal-2"]
    started = []
    release = threading.Event()

    def process(raw):
        started.append(raw)
        release.wait(2)

    monkeypatch.setattr(orchestrator, "process_goal_safe", process)
    futures = set()
    with ThreadPoolExecutor(max_workers=2) as executor:
        orchestrator.submit_queued_goals(executor, futures)
        deadline = time.time() + 1
        while len(started) < 2 and time.time() < deadline:
            time.sleep(0.01)
        release.set()
        for future in futures:
            future.result()

    assert started == ["goal-1", "goal-2"]


def test_failed_dependency_stays_blocked_and_release_is_idempotent(monkeypatch):
    fake = install_redis(monkeypatch)
    fake.hashes.update({
        "sid:jobs:parent": {"id": "parent", "status": "failed"},
        "sid:jobs:child": {
            "id": "child", "status": "blocked",
            "dependencies": json.dumps(["parent"]),
            "prompt": "child", "created_at": "1",
        },
    })
    orchestrator.release_dependencies()
    assert fake.hget("sid:jobs:child", "status") == "blocked_failed_dependency"
    assert fake.queues.get(orchestrator.JOB_QUEUE, []) == []

    fake.hashes["sid:jobs:ready-child"] = {
        "id": "ready-child", "status": "blocked",
        "dependencies": json.dumps(["parent"]),
        "prompt": "ready", "created_at": "1",
    }
    fake.hset("sid:jobs:parent", mapping={"status": "merged"})
    orchestrator.release_dependencies()
    orchestrator.release_dependencies()
    assert len(fake.queues[orchestrator.JOB_QUEUE]) == 1


def test_completed_goal_does_not_change_other_active_goal(monkeypatch):
    fake = install_redis(monkeypatch)
    fake.hashes.update({
        "sid:goals:done": {"status": "running", "jobs": '["done-job"]'},
        "sid:jobs:done-job": {"status": "merged"},
        "sid:goals:active": {"status": "running", "jobs": '["active-job"]'},
        "sid:jobs:active-job": {"status": "claimed"},
    })
    orchestrator.update_goals()
    assert fake.hget("sid:goals:done", "status") == "completed"
    assert fake.hget("sid:goals:active", "status") == "running"


def test_duplicate_dispatch_is_suppressed(monkeypatch):
    fake = install_redis(monkeypatch)
    job = {"id": "job-1"}
    payload = {"id": "job-1", "prompt": "work"}
    assert orchestrator.dispatch_job_once(job, payload)
    assert not orchestrator.dispatch_job_once(job, payload)
    assert len(fake.queues[orchestrator.JOB_QUEUE]) == 1


def exhausted_builder(**extra):
    record = {
        "id": "b1", "role": "builder", "status": "awaiting_review",
        "review_status": "complete", "review_verdict": "changes_required",
        "review_job_id": "rv1", "repair_attempts": "2",
    }
    record.update(extra)
    return record


def test_repair_exhaustion_hands_off_to_human(monkeypatch):
    fake = install_redis(monkeypatch)
    fake.hashes["sid:jobs:b1"] = exhausted_builder()
    fake.hashes["sid:jobs:rv1"] = {"role": "reviewer", "status": "review_complete"}
    orchestrator.queue_repairs()
    builder = fake.hashes["sid:jobs:b1"]
    assert builder["status"] == "needs_human"
    assert builder["repair_status"] == "exhausted"
    assert fake.queues.get(orchestrator.JOB_QUEUE, []) == []
    # needs_human is not a dependency failure: children keep waiting.
    fake.hashes["sid:jobs:child"] = {
        "id": "child", "status": "blocked",
        "dependencies": json.dumps(["b1"]), "prompt": "c", "created_at": "1",
    }
    orchestrator.release_dependencies()
    assert fake.hget("sid:jobs:child", "status") == "blocked"


def test_extended_repair_limit_dispatches_another_repair(monkeypatch):
    fake = install_redis(monkeypatch)
    fake.hashes["sid:jobs:b1"] = exhausted_builder(max_repair_attempts="3")
    fake.hashes["sid:jobs:rv1"] = {"role": "reviewer", "status": "review_complete"}
    orchestrator.queue_repairs()
    builder = fake.hashes["sid:jobs:b1"]
    assert builder["status"] == "awaiting_review"
    assert builder["repair_attempts"] == "3"
    queued = [json.loads(item) for item in fake.queues[orchestrator.JOB_QUEUE]]
    assert [item["role"] for item in queued] == ["repair"]
    assert "3 of 3" in queued[0]["prompt"]


def test_dependency_failure_cascades_to_grandchildren(monkeypatch):
    fake = install_redis(monkeypatch)
    fake.hashes.update({
        "sid:jobs:parent": {"id": "parent", "status": "blocked_failed_dependency"},
        "sid:jobs:grandchild": {
            "id": "grandchild", "status": "blocked",
            "dependencies": json.dumps(["parent"]),
            "prompt": "g", "created_at": "1",
        },
    })
    orchestrator.release_dependencies()
    assert fake.hget("sid:jobs:grandchild", "status") == "blocked_failed_dependency"
