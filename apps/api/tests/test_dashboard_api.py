import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
from agent_routes import get_agent_db
from database import SessionLocal
from models import Base


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)

    def database():
        with sessions() as session:
            yield session

    main.app.dependency_overrides[main.get_db] = database
    main.app.dependency_overrides[get_agent_db] = database
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()
    engine.dispose()


class FakeRedis:
    def __init__(self, hashes, queue_depth=0):
        self.hashes = hashes
        self.queue_depth = queue_depth

    def scan_iter(self, pattern):
        prefix = pattern[:-1]
        return iter(sorted(key for key in self.hashes if key.startswith(prefix)))

    def hgetall(self, key):
        value = self.hashes.get(key, {})
        if isinstance(value, Exception):
            raise value
        return value

    def llen(self, _key):
        return self.queue_depth


def dashboard_redis(monkeypatch):
    fake = FakeRedis({
        "sid:orchestrators:one": {
            "id": "one", "status": "idle", "goal_id": "g1", "last_seen": "bad",
        },
        "sid:workers:w1": {"id": "w1", "role": "builder", "status": "working", "model": "m"},
        "sid:goals:g1": {
            "id": "g1", "goal": "line one\n" + "x" * 400, "status": "running",
            "jobs": '["j1"]', "updated_at": "20",
        },
        "sid:jobs:j1": {
            "id": "j1", "status": "awaiting_review", "role": "builder", "worker_id": "w1",
            "review_status": "complete", "review_verdict": "pass",
            "review_job_id": "review-j1", "integration_status": "passed",
            "integration_base_commit": main.subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "integrated_candidate_commit": "candidate-j1",
            "reviewed_commit": "candidate-j1",
            "integration_worktree": "/opt/sid-worktrees/job-j1-integration",
            "integration_branch": "sid/integration-j1",
            "input_tokens": "100", "cached_input_tokens": "25",
            "output_tokens": "30", "updated_at": "10",
        },
        "sid:jobs:review-j1": {
            "id": "review-j1", "role": "reviewer",
            "builder_job_id": "j1", "status": "review_complete",
            "review_verdict": "pass", "candidate_commit": "candidate-j1",
            "reviewed_commit": "candidate-j1", "updated_at": "9",
        },
        "sid:jobs:bad": {"status": "failed", "input_tokens": "not-a-number", "updated_at": "30"},
        "sid:jobs:missing": None,
    }, queue_depth=3)
    monkeypatch.setattr(main, "redis", fake)
    return fake


def test_dashboard_endpoints_and_bounded_normalization(client, monkeypatch):
    dashboard_redis(monkeypatch)

    assert client.get("/api/repository").status_code == 200
    assert client.get("/api/queue").json() == {"name": "sid:jobs", "depth": 3}
    assert client.get("/api/orchestrators").json()[0]["status"] == "active"
    assert client.get("/api/workers").json()[0]["job_id"] == "j1"
    assert set(client.get("/api/heartbeat").json()) == {"workers", "orchestrators"}
    assert len(client.get("/api/goals").json()[0]["summary"]) == main.GOAL_SUMMARY_LIMIT
    assert "\n" not in client.get("/api/goals").json()[0]["prompt"]
    jobs = client.get("/api/jobs").json()
    j1 = next(item for item in jobs if item["id"] == "j1")
    assert j1["effective_tokens"] == 105
    assert client.get("/api/jobs/recent?limit=1").json()[0]["id"] == "bad"
    assert client.get("/api/approvals").json()[0]["id"] == "j1"
    assert client.get("/api/jobs/approvals").json()[0]["id"] == "j1"
    assert client.get("/api/failures").json()[0]["id"] == "bad"

    status = client.get("/api/status").json()
    assert status["queue"]["depth"] == 3
    assert status["active_goal"]["id"] == "g1"
    assert status["pending_human_approvals"][0]["id"] == "j1"


def test_approvals_are_not_limited_by_recent_jobs_and_bad_state_is_safe(client, monkeypatch):
    fake = dashboard_redis(monkeypatch)
    fake.hashes["sid:jobs:approval"] = {
        "id": "approval",
        "status": "awaiting_review",
        "review_status": "complete",
        "review_verdict": "pass",
        "review_job_id": "review-approval",
        "integration_status": "passed",
        "integration_base_commit": main.subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "integrated_candidate_commit": "candidate-approval",
        "reviewed_commit": "candidate-approval",
        "integration_worktree": "/opt/sid-worktrees/job-approval-integration",
        "integration_branch": "sid/integration-approval",
        "updated_at": "1",
    }
    fake.hashes["sid:jobs:review-approval"] = {
        "id": "review-approval",
        "role": "reviewer",
        "builder_job_id": "approval",
        "status": "review_complete",
        "review_verdict": "pass",
        "candidate_commit": "candidate-approval",
        "reviewed_commit": "candidate-approval",
        "updated_at": "0",
    }
    for index in range(12):
        fake.hashes[f"sid:jobs:recent-{index}"] = {"status": "queued", "updated_at": str(100 + index)}

    assert client.get("/api/jobs?limit=2").json()[0]["id"] == "recent-11"
    assert [item["id"] for item in client.get("/api/approvals").json()] == ["j1", "approval"]
    malformed = client.get("/api/jobs?limit=100").json()
    bad = next(item for item in malformed if item["id"] == "bad")
    assert bad["effective_tokens"] is None
    assert client.get("/api/goals?limit=0").json() == []


def test_dashboard_pagination_supports_offset_and_rejects_negative_offset(client, monkeypatch):
    fake = dashboard_redis(monkeypatch)
    for index in range(5):
        fake.hashes[f"sid:jobs:page-{index}"] = {
            "status": "queued", "updated_at": str(100 + index),
        }
        fake.hashes[f"sid:goals:page-{index}"] = {
            "goal": f"goal {index}", "updated_at": str(100 + index),
        }

    for path in ("/api/jobs", "/api/jobs/recent", "/api/goals", "/api/goals/recent"):
        assert client.get(f"{path}?limit=2&offset=0").json() == client.get(f"{path}?limit=2").json()
        assert [item["id"] for item in client.get(f"{path}?limit=2&offset=2").json()] == ["page-2", "page-1"]
        assert client.get(f"{path}?limit=2&offset=100").json() == []
        assert client.get(f"{path}?offset=-1").status_code == 422



def test_approval_requires_exact_integrated_review_metadata(client, monkeypatch):
    fake = dashboard_redis(monkeypatch)

    fake.hashes["sid:jobs:unsafe"] = {
        "id": "unsafe",
        "status": "awaiting_review",
        "review_status": "complete",
        "review_verdict": "pass",
        "updated_at": "999",
    }

    ids = [item["id"] for item in client.get("/api/approvals").json()]
    assert "j1" in ids
    assert "unsafe" not in ids


class WritableFakeRedis(FakeRedis):
    def __init__(self, hashes=None, queue_depth=0):
        super().__init__(hashes or {}, queue_depth)
        self.values = {}
        self.queues = {}
        self.fail_rpush = False

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)
        self.hashes.pop(key, None)

    def hset(self, key, mapping=None, **kwargs):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def rpush(self, key, value):
        if self.fail_rpush:
            raise RuntimeError("queue unavailable")
        self.queues.setdefault(key, []).append(value)
        return len(self.queues[key])


def test_goal_duplicate_guard_distinguishes_atomic_mode(client, monkeypatch):
    fake = WritableFakeRedis({
        "sid:goals:existing": {
            "id": "existing",
            "goal": "same work",
            "status": "queued",
            "atomic": "false",
        }
    })
    monkeypatch.setattr(main, "redis", fake)

    response = client.post(
        "/api/goals",
        json={"goal": "same work", "atomic": True},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["id"] != "existing"
    assert body["atomic"] is True
    assert len(fake.queues["sid:goals"]) == 1


def test_failed_queue_releases_request_id_reservation(client, monkeypatch):
    fake = WritableFakeRedis()
    fake.fail_rpush = True
    monkeypatch.setattr(main, "redis", fake)

    response = client.post(
        "/api/goals",
        json={
            "goal": "retryable work",
            "atomic": True,
            "request_id": "retry-me",
        },
    )

    assert response.status_code == 503
    assert fake.get("sid:goal-requests:retry-me") is None


def test_request_id_duplicate_rejects_atomic_mode_mismatch(client, monkeypatch):
    fake = WritableFakeRedis({
        "sid:goals:existing": {
            "id": "existing",
            "goal": "original work",
            "status": "queued",
            "atomic": "false",
        }
    })
    fake.values["sid:goal-requests:same-request"] = "existing"
    monkeypatch.setattr(main, "redis", fake)

    response = client.post(
        "/api/goals/submit-atomic",
        json={
            "goal": "original work",
            "request_id": "same-request",
        },
    )

    assert response.status_code == 409


def test_request_id_duplicate_returns_persisted_atomic_mode(client, monkeypatch):
    fake = WritableFakeRedis({
        "sid:goals:existing": {
            "id": "existing",
            "goal": "original work",
            "status": "queued",
            "atomic": "true",
        }
    })
    fake.values["sid:goal-requests:same-request"] = "existing"
    monkeypatch.setattr(main, "redis", fake)

    response = client.post(
        "/api/goals",
        json={
            "goal": "original work",
            "atomic": True,
            "request_id": "same-request",
        },
    )

    assert response.status_code == 202
    assert response.json()["id"] == "existing"
    assert response.json()["atomic"] is True
