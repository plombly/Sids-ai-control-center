"""Operator action endpoints: the API records requests, it never acts."""

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
from agent_routes import get_agent_db
from models import Base

CANDIDATE = "c" * 40
JOB_KEY = "sid:jobs:b1"


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


class OperatorFakeRedis:
    def __init__(self, hashes):
        self.hashes = hashes
        self.stream = []
        self.fail_xadd = False

    def scan_iter(self, pattern):
        prefix = pattern[:-1]
        return iter(sorted(k for k in self.hashes if k.startswith(prefix)))

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hsetnx(self, key, field, value):
        record = self.hashes.setdefault(key, {})
        if field in record:
            return 0
        record[field] = value
        return 1

    def hset(self, key, mapping=None, **kwargs):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def xadd(self, name, fields, maxlen=None, approximate=True):
        if self.fail_xadd:
            raise RuntimeError("stream unavailable")
        self.stream.append((name, dict(fields), maxlen))
        return f"{len(self.stream)}-0"


def heartbeat(allowed="approve,queue_approve,dequeue_approve,reject,extend,reintegrate,reopen"):
    return {"id": "sid-operator-01", "status": "idle", "allowed_actions": allowed,
            "request_ttl": "600", "last_seen": "1"}


@pytest.fixture
def fake(monkeypatch):
    fake = OperatorFakeRedis({
        "sid:operator-service:sid-operator-01": heartbeat(),
        JOB_KEY: {"id": "b1", "status": "awaiting_review", "project_id": "shop",
                  "integrated_candidate_commit": CANDIDATE},
    })
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(main, "OPERATOR_TOKEN", TOKEN)
    return fake


TOKEN = "operator-test-token"
AUTH = {"X-SID-Token": TOKEN}


def body(**fields):
    return {"action": "approve", "request_id": "req-00000001",
            "expected_status": "awaiting_review", "expected_candidate": CANDIDATE,
            **fields}


def post(client, job_id="b1", headers=AUTH, **fields):
    return client.post(f"/api/jobs/{job_id}/actions", json=body(**fields), headers=headers)


@pytest.mark.parametrize("fields,extra", [
    ({}, ""),
    ({"action": "reject", "expected_candidate": None}, ""),
    ({"action": "extend", "expected_candidate": None, "extra": 2}, "2"),
    ({"action": "reintegrate", "expected_candidate": None}, ""),
    ({"action": "reopen", "expected_candidate": None}, ""),
])
def test_valid_request_is_recorded_pending_and_queued(client, fake, fields, extra):
    response = post(client, **fields)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pending"
    [(name, queued, maxlen)] = fake.stream
    assert name == "sid:operator-requests"
    assert maxlen == main.OPERATOR_STREAM_MAXLEN
    assert queued["job_id"] == "b1"
    assert queued["action"] == body(**fields)["action"]
    assert queued["extra"] == extra
    result = fake.hashes["sid:operator-results:req-00000001"]
    assert result["status"] == "pending"
    assert result["expected_candidate"] == (CANDIDATE if "action" not in fields else "")


def test_api_never_writes_job_state(client, fake):
    before = copy.deepcopy(fake.hashes[JOB_KEY])
    post(client)
    post(client, request_id="req-00000002", action="reject", expected_candidate=None)
    assert fake.hashes[JOB_KEY] == before


INVALID_BODIES = [
    ("approve without candidate", {"expected_candidate": None}),
    ("short candidate", {"expected_candidate": CANDIDATE[:12]}),
    ("uppercase candidate", {"expected_candidate": CANDIDATE.upper()}),
    ("candidate on reject", {"action": "reject"}),
    ("extend without extra", {"action": "extend", "expected_candidate": None}),
    ("extend too many", {"action": "extend", "expected_candidate": None, "extra": 6}),
    ("extra on reject", {"action": "reject", "expected_candidate": None, "extra": 1}),
    ("unknown action", {"action": "merge"}),
    ("short request id", {"request_id": "abc"}),
    ("bad request id", {"request_id": "../../../etc/x"}),
    ("missing expected status", {"expected_status": ""}),
    ("unknown field", {"force": True}),
]


@pytest.mark.parametrize("name,fields", INVALID_BODIES, ids=[c[0] for c in INVALID_BODIES])
def test_invalid_request_is_rejected_and_not_queued(client, fake, name, fields):
    assert post(client, **fields).status_code == 422
    assert fake.stream == []


@pytest.mark.parametrize("job_id", ["salvage-api-22178db", "a" * 65, "b1.x"])
def test_invalid_job_id_is_rejected(client, fake, job_id):
    assert post(client, job_id=job_id).status_code in {404, 422}
    assert fake.stream == []


def test_offline_operator_service_queues_nothing(client, fake):
    del fake.hashes["sid:operator-service:sid-operator-01"]
    response = post(client)
    assert response.status_code == 503
    assert "job-review.py" in response.json()["detail"]
    assert fake.stream == []
    assert "sid:operator-results:req-00000001" not in fake.hashes


def test_action_disabled_on_host_is_forbidden(client, fake):
    fake.hashes["sid:operator-service:sid-operator-01"] = heartbeat("reject,reopen")
    assert post(client).status_code == 403
    assert fake.stream == []


def test_missing_job_is_404(client, fake):
    assert post(client, job_id="nojob").status_code == 404
    assert fake.stream == []


def test_stale_view_of_job_status_is_409(client, fake):
    fake.hashes[JOB_KEY]["status"] = "merged"
    response = post(client)
    assert response.status_code == 409
    assert "refresh" in response.json()["detail"]
    assert fake.stream == []


def test_retry_with_same_request_returns_existing_result_without_requeue(client, fake):
    assert post(client).status_code == 202
    fake.hashes["sid:operator-results:req-00000001"]["status"] = "succeeded"
    # Even after the job changed and the service went offline.
    fake.hashes[JOB_KEY]["status"] = "merged"
    del fake.hashes["sid:operator-service:sid-operator-01"]
    response = post(client)
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert len(fake.stream) == 1


def test_same_request_id_for_different_request_is_409(client, fake):
    assert post(client).status_code == 202
    response = post(client, action="reject", expected_candidate=None)
    assert response.status_code == 409
    assert len(fake.stream) == 1


def test_stream_failure_marks_request_queue_failed(client, fake):
    fake.fail_xadd = True
    assert post(client).status_code == 503
    assert fake.hashes["sid:operator-results:req-00000001"]["status"] == "queue_failed"


def test_requested_from_is_recorded_for_audit(client, fake):
    client.post("/api/jobs/b1/actions", json=body(), headers={**AUTH, "X-Real-IP": "192.0.2.7"})
    assert fake.hashes["sid:operator-results:req-00000001"]["requested_from"] == "192.0.2.7"
    assert fake.stream[0][1]["requested_from"] == "192.0.2.7"


def test_get_operator_request(client, fake):
    assert client.get("/api/operator-requests/req-00000001").status_code == 404
    assert client.get("/api/operator-requests/bad!id").status_code == 422
    post(client)
    fake.hashes["sid:operator-results:req-00000001"].update(
        status="refused", message="stale main: reintegrate")
    result = client.get("/api/operator-requests/req-00000001").json()
    assert result["status"] == "refused"
    assert result["message"] == "stale main: reintegrate"
    assert result["expected_candidate"] == CANDIDATE


def test_operator_status(client, fake):
    status = client.get("/api/operator/status").json()
    assert status["online"] is True
    assert "approve" in status["allowed_actions"]
    del fake.hashes["sid:operator-service:sid-operator-01"]
    assert client.get("/api/operator/status").json() == {"online": False, "allowed_actions": []}


def test_concurrent_submit_of_same_request_id_queues_once(client, fake, monkeypatch):
    # Another submission reserved the id between our read and our reserve.
    monkeypatch.setattr(fake, "hsetnx", lambda key, field, value: 0)
    response = post(client)
    assert response.status_code == 409
    assert fake.stream == []


def test_operator_endpoints_have_no_repository_authority():
    source = Path(main.__file__).read_text()
    start = source.index("# Operator actions.")
    end = source.index("def _redact(")
    section = source[start:end]
    for forbidden in ("subprocess", '"git"', "rpush(", "sid:jobs:{"):
        assert forbidden not in section, forbidden


# --- write access control ------------------------------------------------------

def test_writes_need_the_operator_token(client, fake):
    assert post(client, headers={}).status_code == 401
    assert post(client, headers={"X-SID-Token": "wrong"}).status_code == 401
    assert fake.stream == []
    assert post(client).status_code == 202


@pytest.mark.parametrize("method,path,payload", [
    ("post", "/api/projects/shop/goals", {"goal": "x"}),
    ("post", "/api/workers/w1/stop", None),
    ("delete", "/api/workers/w1", None),
    ("post", "/projects", {"name": "p"}),
    ("post", "/api/dismissals", {"ids": ["j1"]}),
    ("delete", "/api/dismissals/j1", None),
])
def test_every_write_route_is_protected(client, fake, method, path, payload):
    kwargs = {"json": payload} if payload is not None else {}
    assert getattr(client, method)(path, **kwargs).status_code == 401


def test_reads_stay_open(client, fake):
    assert client.get("/api/operator/status").status_code == 200
    assert client.get("/api/jobs").status_code == 200


def test_auth_status_reports_requirement_and_validity(client, fake, monkeypatch):
    assert client.get("/api/auth").json() == {"token_required": True, "token_valid": False}
    assert client.get("/api/auth", headers=AUTH).json() == {"token_required": True, "token_valid": True}
    monkeypatch.setattr(main, "OPERATOR_TOKEN", "")
    assert client.get("/api/auth", headers=AUTH).json() == {"token_required": False, "token_valid": False}


def test_web_approval_is_refused_without_a_configured_token(client, fake, monkeypatch):
    monkeypatch.setattr(main, "OPERATOR_TOKEN", "")
    response = post(client, headers={})
    assert response.status_code == 403
    assert "SID_OPERATOR_TOKEN" in response.json()["detail"]
    assert fake.stream == []
    # Other actions keep working without a token (unchanged behavior).
    assert post(client, headers={}, action="reject", expected_candidate=None).status_code == 202


# --- merge queue -------------------------------------------------------------------

def test_queue_approve_needs_candidate_and_token(client, fake, monkeypatch):
    assert post(client, action="queue_approve", expected_candidate=None).status_code == 422
    assert post(client, action="queue_approve").status_code == 202
    assert fake.stream[-1][1]["action"] == "queue_approve"
    monkeypatch.setattr(main, "OPERATOR_TOKEN", "")
    response = post(client, headers={}, request_id="req-00000009", action="queue_approve")
    assert response.status_code == 403 and "SID_OPERATOR_TOKEN" in response.json()["detail"]


def test_dequeue_needs_no_candidate(client, fake):
    assert post(client, action="dequeue_approve", expected_candidate=None).status_code == 202
    assert post(client, request_id="req-00000002", action="dequeue_approve").status_code == 422


def test_merge_queue_read_model(client, fake, monkeypatch):
    class Lists(OperatorFakeRedis):
        def lrange(self, key, start, end):
            return ["b1", "gone"] if key == "sid:merge-queue" else []

        def get(self, key):
            return "h" * 40 if key == "sid:main-head" else None

    queue = Lists(fake.hashes)
    fake.hashes["sid:jobs:b1"].update({
        "title": "Add x", "merge_queue_state": "waiting", "merge_queue_reason": "stale: waiting",
        "approval_intent_candidate": "a" * 40, "integration_base_commit": "h" * 40,
        "approval_intent_at": "12"})
    monkeypatch.setattr(main, "redis", queue)
    body = client.get("/api/merge-queue").json()
    assert body["main_head"] == "h" * 40
    first, second = body["items"]
    assert (first["position"], first["id"], first["state"], first["fresh"]) == (1, "b1", "waiting", True)
    assert first["approved_candidate"] == "a" * 40 and first["candidate"] == CANDIDATE
    assert (second["id"], second["status"], second["state"]) == ("gone", "unknown", "queued")


# --- parallel-pipeline read models ------------------------------------------------------

def test_job_exposes_pipeline_state(client, fake, monkeypatch):
    fake.hashes["sid:jobs:b1"].update({
        "blocked_reason": "waiting for apps/web/app.js held by job x (running)",
        "build_attempt": "2", "provider_fallback": "claude at capacity", "cost_usd": "0.0421",
        "best_of": '{"chosen": "alt"}', "review_aspects": '{"spec": "pass", "safety": "changes_required"}',
        "merge_queue_state": "waiting", "merge_queue_reason": "stale"})

    class Scan(OperatorFakeRedis):
        def hgetall(self, key):
            return dict(self.hashes.get(key, {}))

    monkeypatch.setattr(main, "redis", Scan(fake.hashes))
    job = next(j for j in client.get("/api/jobs?limit=100").json() if j["id"] == "b1")
    assert job["blocked_reason"].startswith("waiting for apps/web/app.js")
    assert (job["build_attempt"], job["cost_usd"]) == (2, 0.0421)
    assert job["best_of"] == {"chosen": "alt"}
    assert job["review_aspects"] == {"spec": "pass", "safety": "changes_required"}
    assert (job["merge_queue_state"], job["provider_fallback"]) == ("waiting", "claude at capacity")
    fake.hashes["sid:jobs:b1"]["best_of"] = "not json"
    assert next(j for j in client.get("/api/jobs?limit=100").json() if j["id"] == "b1")["best_of"] is None


def test_providers_capacity(client, fake, monkeypatch):
    class Slots(OperatorFakeRedis):
        def zrangebyscore(self, key, lo, hi, withscores=False):
            return [("job:rv1", 9999999999.0)]

        def get(self, key):
            return {"sid:provider-limit:claude": "2",
                    "sid:provider-cooldown:claude": "claude unavailable: limit"}.get(key)

        def ttl(self, key):
            return 1200

    fake.hashes["sid:workers:w1"] = {"id": "w1", "provider": "per role",
                                     "model": "builder codex/gpt · reviewer claude/sonnet"}
    monkeypatch.setattr(main, "redis", Slots(fake.hashes))
    body = client.get("/api/providers").json()
    assert body["claude"]["limit"] == 2
    assert body["claude"]["in_use"] == [{"holder": "job:rv1", "lease_expires": 9999999999}]
    assert body["claude"]["cooling_down"] is True and body["claude"]["cooldown_seconds_left"] == 1200
    assert body["routing"] == "builder codex/gpt · reviewer claude/sonnet"


def test_reviewer_records_specialist_verdicts():
    source = (Path(main.__file__).resolve().parents[2] / "services/worker/worker.py").read_text()
    assert '"review_aspects": review_aspects_json' in source


def test_system_health_passes_through_the_watchdog_report(client, fake, monkeypatch):
    import time as _time

    class Health(OperatorFakeRedis):
        def __init__(self, hashes, values):
            super().__init__(hashes)
            self.values = values

        def get(self, key):
            return self.values.get(key)

    report = {"status": "warn", "checked_at": _time.time() - 30,
              "checks": [{"name": "backup", "level": "warn", "detail": "no backup recorded yet"}]}
    monkeypatch.setattr(main, "redis", Health(fake.hashes, {
        "sid:health": json.dumps(report),
        "sid:backup:last": json.dumps({"at": "20260930T192958Z", "ok": True})}))
    body = client.get("/api/system-health").json()
    assert body["report"]["status"] == "warn" and 25 <= body["report"]["age_seconds"] <= 40
    assert body["report"]["checks"][0]["name"] == "backup"
    assert body["backup"] == {"at": "20260930T192958Z", "ok": True}
    monkeypatch.setattr(main, "redis", Health(fake.hashes, {"sid:health": "not json"}))
    assert client.get("/api/system-health").json() == {"report": None, "backup": None}
