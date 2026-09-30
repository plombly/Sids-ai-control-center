"""Behavior of services/operator/sid_operator.py, the Web action executor."""

import pytest

from sid_testing import BASE, INTEGRATED, JOB, ROOT, load_module, make_builder

STREAM = "sid:operator-requests"
GROUP = "sid-operator"
NOW = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def clock(memory_redis):
    clock = Clock()
    memory_redis.clock = clock
    return clock


@pytest.fixture
def op(memory_redis, job_review, clock):
    module = load_module(ROOT / "services/operator/sid_operator.py")
    module.redis = memory_redis
    module.job_review = job_review
    module.ALLOWED_ACTIONS = frozenset(module.ACTIONS)
    module.ensure_group()
    return module


@pytest.fixture
def calls(op, monkeypatch):
    """Replace every job-review action with a recorder."""
    seen = []
    for name in ("approve", "reject", "extend", "reintegrate", "reopen"):
        def record(*args, _name=name, **kwargs):
            seen.append((_name, args, kwargs))
            print(f"{_name.upper()}: done")
        monkeypatch.setattr(op.job_review, name, record)
    return seen


def submit(op, request_id="req-00000001", **fields):
    request = {"request_id": request_id, "job_id": JOB, "action": "reject",
               "expected_status": "awaiting_review", **fields}
    return op.redis.xadd(STREAM, request)


def deliver(op, now=None):
    """One pass of the service loop: read new entries and process them."""
    replies = op.redis.xreadgroup(GROUP, op.OPERATOR_ID, {STREAM: ">"}, count=100)
    for _stream, messages in replies:
        for entry_id, fields in messages:
            op.process(entry_id, fields, NOW if now is None else now)


def result(op, request_id="req-00000001"):
    return op.redis.hgetall(f"sid:operator-results:{request_id}")


def job(op, status="awaiting_review", **fields):
    return make_builder(op.job_review, status, **fields)


# --- dispatch ------------------------------------------------------------------

@pytest.mark.parametrize("fields,expected", [
    ({"action": "approve", "expected_candidate": INTEGRATED},
     ("approve", (JOB,), {"expected_candidate": INTEGRATED})),
    ({"action": "reject"}, ("reject", (JOB,), {})),
    ({"action": "extend", "extra": "3", "expected_status": "needs_human"},
     ("extend", (JOB, 3), {})),
    ({"action": "reintegrate"}, ("reintegrate", (JOB,), {})),
    ({"action": "reopen", "expected_status": "blocked_failed_dependency"},
     ("reopen", (JOB,), {})),
])
def test_each_action_calls_job_review_with_its_arguments(op, calls, fields, expected):
    job(op, fields.get("expected_status", "awaiting_review"))
    submit(op, **fields)
    deliver(op)
    assert calls == [expected]
    done = result(op)
    assert done["status"] == "succeeded"
    assert done["message"] == f"{expected[0].upper()}: done"
    assert done["service_id"] == op.OPERATOR_ID
    assert op.redis.pending(STREAM, GROUP) == {}


def test_approve_end_to_end_merges_the_confirmed_candidate(op, fake_git, approvable):
    submit(op, action="approve", expected_candidate=INTEGRATED)
    deliver(op)
    assert result(op)["status"] == "succeeded"
    assert fake_git.ran("merge") == [("merge", "--ff-only", f"sid/integration-{JOB}")]
    assert op.redis.hget(f"sid:jobs:{JOB}", "status") == "merged"


def test_approve_end_to_end_refuses_a_candidate_the_human_did_not_see(
        op, fake_git, approvable):
    submit(op, action="approve", expected_candidate="d" * 40)
    deliver(op)
    done = result(op)
    assert done["status"] == "refused"
    assert "is not the integrated candidate" in done["message"]
    assert "ERROR:" in done["output"]
    assert fake_git.ran("merge") == []
    assert op.redis.hget(f"sid:jobs:{JOB}", "status") == "awaiting_review"


def test_approve_end_to_end_reports_stale_main(op, fake_git, approvable):
    fake_git.main_head = "e" * 40
    submit(op, action="approve", expected_candidate=INTEGRATED)
    deliver(op)
    assert result(op)["status"] == "refused"
    assert "stale main" in result(op)["message"]
    assert fake_git.ran("merge") == []


def test_reject_end_to_end(op, fake_git):
    job(op, "needs_human")
    submit(op, expected_status="needs_human")
    deliver(op)
    assert result(op)["status"] == "succeeded"
    assert result(op)["message"] == f"REJECTED: {JOB}"
    assert op.redis.hget(f"sid:jobs:{JOB}", "status") == "rejected"


def test_job_review_refusal_is_recorded_with_its_reason(op, fake_git):
    job(op, "needs_human")  # extend needs a CHANGES_REQUIRED review
    submit(op, action="extend", extra="1", expected_status="needs_human")
    deliver(op)
    done = result(op)
    assert done["status"] == "refused"
    assert "no current CHANGES_REQUIRED review" in done["message"]


# --- validation: nothing runs ----------------------------------------------------

INVALID = [
    ("unknown action", {"action": "merge"}, "unknown action"),
    ("path traversal id", {"job_id": "../etc"}, "invalid job id"),
    ("non-alphanumeric id", {"job_id": "salvage-api-22178db"}, "invalid job id"),
    ("empty id", {"job_id": ""}, "invalid job id"),
    ("overlong id", {"job_id": "a" * 65}, "invalid job id"),
    ("no expected status", {"expected_status": ""}, "expected_status is required"),
    ("approve without candidate", {"action": "approve"}, "full 40-character"),
    ("approve short candidate", {"action": "approve", "expected_candidate": INTEGRATED[:12]}, "full 40-character"),
    ("approve uppercase candidate", {"action": "approve", "expected_candidate": INTEGRATED.upper()}, "full 40-character"),
    ("candidate on reject", {"expected_candidate": INTEGRATED}, "only accepted for approve"),
    ("extend without extra", {"action": "extend"}, "between 1 and 5"),
    ("extend zero", {"action": "extend", "extra": "0"}, "between 1 and 5"),
    ("extend six", {"action": "extend", "extra": "6"}, "between 1 and 5"),
    ("extend text", {"action": "extend", "extra": "two"}, "between 1 and 5"),
    ("extra on reject", {"extra": "1"}, "only accepted for extend"),
]


@pytest.mark.parametrize("name,fields,reason", INVALID, ids=[case[0] for case in INVALID])
def test_invalid_requests_are_refused_without_running(op, calls, name, fields, reason):
    job(op)
    submit(op, **fields)
    deliver(op)
    assert calls == []
    assert result(op)["status"] == "refused"
    assert reason.lower() in result(op)["message"].lower()
    assert op.redis.pending(STREAM, GROUP) == {}


def test_disabled_action_is_refused(op, calls):
    op.ALLOWED_ACTIONS = frozenset({"reject"})
    job(op)
    submit(op, action="approve", expected_candidate=INTEGRATED)
    deliver(op)
    assert calls == []
    assert "disabled on this host" in result(op)["message"]


def test_default_allowlist_excludes_approve(op):
    assert op.parse_allowed_actions(op.DEFAULT_ALLOWED_ACTIONS) == frozenset(
        {"reject", "extend", "reintegrate", "reopen"})


def test_unknown_allowlist_entry_stops_startup(op):
    with pytest.raises(SystemExit):
        op.parse_allowed_actions("reject,approve-all")


@pytest.mark.parametrize("request_id", ["", "short", "../../x", "a" * 65, "-leading-dash"])
def test_unkeyable_request_is_dropped_without_a_result(op, calls, request_id):
    job(op)
    submit(op, request_id=request_id)
    deliver(op)
    assert calls == []
    assert not [k for k in op.redis.records if k.startswith("sid:operator-results:")]
    assert op.redis.pending(STREAM, GROUP) == {}


def test_job_status_changed_since_the_human_looked(op, calls):
    job(op, "needs_human")
    submit(op, expected_status="awaiting_review")
    deliver(op)
    assert calls == []
    assert "refresh and decide again" in result(op)["message"]


def test_missing_job_is_refused(op, calls):
    submit(op)
    deliver(op)
    assert calls == []
    assert result(op)["message"] == f"job not found: {JOB}"


def test_expired_request_never_runs(op, calls, clock):
    job(op)
    clock.now = NOW - op.REQUEST_TTL - 1
    submit(op)
    deliver(op, now=NOW)
    assert calls == []
    assert result(op)["status"] == "expired"


def test_request_just_inside_ttl_runs(op, calls, clock):
    job(op)
    clock.now = NOW - op.REQUEST_TTL + 1
    submit(op)
    deliver(op, now=NOW)
    assert len(calls) == 1


# --- at most once -----------------------------------------------------------------

def test_redelivered_entry_runs_once(op, calls):
    job(op)
    entry = submit(op)
    deliver(op)
    first = result(op)
    op.process(entry, op.redis.streams[STREAM][0][1], NOW)
    assert len(calls) == 1
    assert result(op) == first


def test_second_entry_with_same_request_id_is_skipped(op, calls):
    job(op)
    submit(op)
    submit(op, action="reintegrate")
    deliver(op)
    assert [call[0] for call in calls] == ["reject"]
    assert result(op)["action"] == "reject"
    assert op.redis.pending(STREAM, GROUP) == {}


def test_api_recorded_request_fields_are_kept(op, calls):
    job(op)
    op.redis.hset("sid:operator-results:req-00000001", mapping={
        "request_id": "req-00000001", "status": "pending",
        "requested_from": "10.0.0.5", "created_at": "1",
    })
    submit(op, requested_from="spoofed")
    deliver(op)
    assert result(op)["requested_from"] == "10.0.0.5"
    assert result(op)["created_at"] == "1"
    assert result(op)["status"] == "succeeded"


# --- failures ---------------------------------------------------------------------

def test_unexpected_exception_is_an_error_and_the_service_continues(op, calls, monkeypatch):
    job(op)

    def boom(job_id):
        raise RuntimeError("disk full")

    monkeypatch.setattr(op.job_review, "reject", boom)
    submit(op)
    submit(op, request_id="req-00000002", action="reintegrate")
    deliver(op)
    assert result(op)["status"] == "error"
    assert result(op)["message"] == "RuntimeError: disk full"
    assert result(op, "req-00000002")["status"] == "succeeded"


def test_plain_system_exit_is_an_error_not_a_crash(op, calls, monkeypatch):
    job(op)

    def usage_exit(job_id):
        raise SystemExit(2)

    monkeypatch.setattr(op.job_review, "reject", usage_exit)
    submit(op)
    deliver(op)
    assert result(op)["status"] == "error"
    assert "code 2" in result(op)["message"]


def test_redis_failure_while_recording_leaves_entry_pending_then_interrupted(
        op, calls, monkeypatch):
    job(op)
    submit(op)
    real_finish = op.finish

    def failing_finish(*args, **kwargs):
        raise ConnectionError("redis went away")

    monkeypatch.setattr(op, "finish", failing_finish)
    with pytest.raises(ConnectionError):
        deliver(op)
    assert len(op.redis.pending(STREAM, GROUP)) == 1, "must not ack an unrecorded result"
    monkeypatch.setattr(op, "finish", real_finish)
    op.recover(now=NOW)
    assert len(calls) == 1, "must not re-run"
    assert result(op)["status"] == "interrupted"
    assert op.redis.pending(STREAM, GROUP) == {}


# --- crash recovery -----------------------------------------------------------------

def read_without_processing(op):
    op.redis.xreadgroup(GROUP, op.OPERATOR_ID, {STREAM: ">"}, count=100)


def test_request_running_at_crash_is_interrupted_not_rerun(op, calls):
    job(op)
    submit(op)
    read_without_processing(op)
    op.redis.hset("sid:operator-results:req-00000001",
                  mapping={"started_at": str(NOW), "status": "running"})
    op.recover(now=NOW)
    assert calls == []
    assert result(op)["status"] == "interrupted"
    assert op.redis.pending(STREAM, GROUP) == {}


def test_claimed_but_not_yet_running_is_interrupted(op, calls):
    job(op)
    submit(op)
    read_without_processing(op)
    op.redis.hset("sid:operator-results:req-00000001",
                  mapping={"started_at": str(NOW), "status": "pending"})
    op.recover(now=NOW)
    assert calls == []
    assert result(op)["status"] == "interrupted"


def test_delivered_but_unclaimed_request_is_processed_on_recovery(op, calls):
    job(op)
    submit(op)
    read_without_processing(op)
    op.recover(now=NOW)
    assert len(calls) == 1
    assert result(op)["status"] == "succeeded"
    assert op.redis.pending(STREAM, GROUP) == {}


def test_finished_but_unacked_request_is_acked_without_rerun(op, calls):
    job(op)
    submit(op)
    read_without_processing(op)
    op.redis.hset("sid:operator-results:req-00000001",
                  mapping={"started_at": str(NOW), "status": "succeeded"})
    op.recover(now=NOW)
    assert calls == []
    assert result(op)["status"] == "succeeded"
    assert op.redis.pending(STREAM, GROUP) == {}


def test_unclaimed_request_that_expired_during_downtime_is_expired(op, calls):
    job(op)
    submit(op)
    read_without_processing(op)
    op.recover(now=NOW + op.REQUEST_TTL + 1)
    assert calls == []
    assert result(op)["status"] == "expired"


# --- service plumbing -----------------------------------------------------------------

def test_heartbeat_advertises_allowed_actions_with_ttl(op):
    op.ALLOWED_ACTIONS = frozenset({"reject", "reopen"})
    op.heartbeat()
    key = f"sid:operator-service:{op.OPERATOR_ID}"
    beat = op.redis.hgetall(key)
    assert beat["allowed_actions"] == "reject,reopen"
    assert beat["status"] == "idle"
    assert op.redis.ttls[key] == 30


def test_ensure_group_is_idempotent(op):
    op.ensure_group()
    op.ensure_group()


def test_service_delegates_to_job_review_and_has_no_git_of_its_own():
    source = (ROOT / "services/operator/sid_operator.py").read_text()
    assert 'ROOT / "scripts/job-review.py"' in source
    assert 'job_review.approve(job_id, expected_candidate=request["expected_candidate"])' in source
    for forbidden in ("subprocess", '"git"', '"merge"', "hdel(", "rpush("):
        assert forbidden not in source, forbidden
