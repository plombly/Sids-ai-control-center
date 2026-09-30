import subprocess

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
    def __init__(self, hashes, queue_depth=0, strings=None):
        self.hashes = hashes
        self.queue_depth = queue_depth
        self.strings = strings or {}
        self.sets = {}

    def scan_iter(self, pattern):
        prefix = pattern[:-1]
        return iter(sorted(key for key in self.hashes if key.startswith(prefix)))

    def hgetall(self, key):
        value = self.hashes.get(key, {})
        if isinstance(value, Exception):
            raise value
        return value

    def get(self, key):
        value = self.strings.get(key)
        if isinstance(value, Exception):
            raise value
        return value

    def llen(self, _key):
        return self.queue_depth

    def sadd(self, key, value):
        values = self.sets.setdefault(key, set())
        added = value not in values
        values.add(value)
        return int(added)

    def srem(self, key, value):
        values = self.sets.setdefault(key, set())
        if value in values:
            values.remove(value)
            return 1
        return 0

    def smembers(self, key):
        return self.sets.get(key, set())

    def scard(self, key):
        return len(self.sets.get(key, set()))


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
            "integration_base_commit": subprocess.check_output(
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


def test_repository_uses_main_head_from_redis(client, monkeypatch):
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(main, "redis", FakeRedis({}, strings={"sid:main-head": sha}))

    response = client.get("/api/repository")

    assert response.status_code == 200
    assert response.json() == {
        "branch": "main", "head": sha, "short": "0123456789ab", "status": "ok",
    }


def test_repository_is_unknown_when_main_head_is_missing(client, monkeypatch):
    monkeypatch.setattr(main, "redis", FakeRedis({}))

    response = client.get("/api/repository")

    assert response.status_code == 200
    assert response.json() == {
        "branch": "unknown", "head": None, "short": None, "status": "unknown",
    }


def test_repository_is_unknown_when_redis_errors(client, monkeypatch):
    monkeypatch.setattr(main, "redis", FakeRedis({}, strings={"sid:main-head": RuntimeError("redis down")}))

    response = client.get("/api/repository")

    assert response.status_code == 200
    assert response.json() == {
        "branch": "unknown", "head": None, "short": None, "status": "unknown",
    }


def test_dismissals_use_only_the_dismissed_set(client, monkeypatch):
    fake = dashboard_redis(monkeypatch)
    before = {key: value.copy() if isinstance(value, dict) else value for key, value in fake.hashes.items()}

    assert client.post("/api/dismissals", json={"ids": ["j2", "g1", "j2"]}).json() == {
        "ids": ["j2", "g1"], "total": 2
    }
    assert client.post("/api/dismissals", json={"ids": ["g1", "j3"]}).json() == {"ids": ["j3"], "total": 3}
    assert client.get("/api/dismissals").json() == {"ids": ["g1", "j2", "j3"]}
    assert client.delete("/api/dismissals/j2").json() == {"id": "j2", "removed": True}
    assert client.delete("/api/dismissals/j2").json() == {"id": "j2", "removed": False}
    assert client.delete("/api/dismissals/bad/id").status_code == 422
    assert client.post("/api/dismissals", json={"ids": []}).status_code == 422
    assert client.post("/api/dismissals", json={"ids": ["j1"] * 501}).status_code == 422
    assert client.post("/api/dismissals", json={"ids": ["bad/id"]}).status_code == 422
    assert fake.hashes == before


def test_approvals_are_not_limited_by_recent_jobs_and_bad_state_is_safe(client, monkeypatch):
    fake = dashboard_redis(monkeypatch)
    fake.hashes["sid:jobs:approval"] = {
        "id": "approval",
        "status": "awaiting_review",
        "review_status": "complete",
        "review_verdict": "pass",
        "review_job_id": "review-approval",
        "integration_status": "passed",
        "integration_base_commit": subprocess.check_output(
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


def test_job_detail_lineage_gate_and_related_jobs(client, monkeypatch):
    fake = dashboard_redis(monkeypatch)
    fake.hashes["sid:jobs:j1"].update({
        "build_attempt": "2", "max_build_attempts": "3", "review_recoveries": "1",
        "retry_reason": "SID test gate failed", "needs_human_kind": "build",
        "last_integrate_job_id": "int-1",
        # order is cherry-pick order and must be kept
        "source_candidate_commits": '["zz-first", "aa-second"]',
        "review_findings_history": '[{"review_job_id":"rv0","candidate":"c0","findings":"fix it"}, 3, "x"]',
        "integration_result": '{"returncode": 1, "stdout": "' + "x" * 3500 + 'END"}',
    })
    fake.hashes["sid:jobs:repair-late"] = {"id": "repair-late", "role": "repair",
                                           "target_builder_id": "j1", "status": "repair_complete",
                                           "created_at": "50"}
    fake.hashes["sid:jobs:int-1"] = {"id": "int-1", "role": "integrate", "target_builder_id": "j1",
                                     "status": "integrate_complete", "created_at": "40"}
    fake.hashes["sid:jobs:other"] = {"id": "other", "role": "reviewer", "builder_job_id": "j9",
                                     "created_at": "45"}

    detail = client.get("/api/jobs/j1").json()

    lineage = detail["lineage"]
    assert lineage["build_attempt"] == 2 and lineage["max_build_attempts"] == 3
    assert lineage["review_recoveries"] == 1
    assert lineage["retry_reason"] == "SID test gate failed"
    assert lineage["needs_human_kind"] == "build"
    assert lineage["last_integrate_job_id"] == "int-1"
    assert lineage["repair_job_id"] is None
    assert lineage["source_candidate_commits"] == ["zz-first", "aa-second"]
    assert lineage["review_findings_history"] == [
        {"review_job_id": "rv0", "candidate": "c0", "findings": "fix it"}]
    assert detail["gate"]["returncode"] == 1
    assert len(detail["gate"]["summary"]) == 3000
    assert detail["gate"]["summary"].endswith("END")
    # review-j1 (fixture reviewer, no created_at) sorts first; "other" is unrelated.
    assert [(j["id"], j["role"]) for j in detail["related"]] == [
        ("review-j1", "reviewer"), ("int-1", "integrate"), ("repair-late", "repair")]
    assert detail["related"][2]["created_at"] == 50
    assert detail["review_status"] == "complete", "existing fields unchanged"


def test_job_detail_lineage_tolerates_missing_and_malformed_fields(client, monkeypatch):
    fake = dashboard_redis(monkeypatch)
    fake.hashes["sid:jobs:j1"].update({
        "source_candidate_commits": "not json",
        "review_findings_history": '{"not": "a list"}',
        "integration_result": "[1, 2]",
    })
    detail = client.get("/api/jobs/j1").json()
    assert detail["lineage"]["source_candidate_commits"] == []
    assert detail["lineage"]["review_findings_history"] == []
    assert detail["lineage"]["build_attempt"] is None
    assert detail["gate"] is None
    fake.hashes["sid:jobs:j1"].pop("integration_result")
    assert client.get("/api/jobs/j1").json()["gate"] is None
    assert client.get("/api/jobs/nojob").status_code == 404
