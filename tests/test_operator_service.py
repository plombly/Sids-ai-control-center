"""Behavior of services/operator/sid_operator.py, the Web action executor."""

import json

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
        {"reject", "extend", "reintegrate", "reopen", "dequeue_approve",
         "network_once", "network_always", "network_deny"})


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
    for forbidden in ('"git"', '"merge"', "hdel(", "rpush("):
        assert forbidden not in source, forbidden
    # Its only subprocess is the project CLI (host-side project management).
    calls = [line.strip() for line in source.splitlines() if "runner(" in line or "subprocess.run" in line]
    assert calls == ["def execute_project(request, runner=subprocess.run):",
                     'result = runner(["/usr/bin/python3", str(PROJECT_CLI), *project_cli_args(request)],'], calls


# --- project actions (host-side project management) ----------------------------------

@pytest.mark.parametrize("fields,reason", [
    ({"action": "create_project", "project_id": "Bad_Id", "name": "x", "source": "empty"}, "invalid project id"),
    ({"action": "create_project", "project_id": "web", "name": "", "source": "empty"}, "1-80"),
    ({"action": "create_project", "project_id": "web", "name": "Web", "source": "zip"}, "empty or clone"),
    ({"action": "create_project", "project_id": "web", "name": "Web", "source": "clone", "url": "/etc"}, "git@"),
    ({"action": "create_project", "project_id": "web", "name": "Web", "source": "clone", "url": "file:///etc"}, "git@"),
    ({"action": "create_project", "project_id": "web", "name": "Web", "source": "empty", "importance": "urgent"}, "importance"),
    ({"action": "create_project", "project_id": "web", "name": "Web", "source": "empty", "gate": "a\nb"}, "one line"),
    ({"action": "create_project", "project_id": "web", "name": "Web", "source": "empty", "push_remote": "/tmp/x"}, "push remote"),
    ({"action": "project_push_setup", "project_id": "web", "url": "../../x"}, "git@"),
    ({"action": "delete_project", "project_id": "web"}, "confirm"),
    ({"action": "delete_project", "project_id": "web", "confirm": "web2"}, "confirm"),
    ({"action": "delete_project", "project_id": "sid", "confirm": "sid"}, "SID itself"),
])
def test_project_requests_are_validated(op, fields, reason):
    with pytest.raises(op.Invalid, match=reason):
        op.validate(fields, f"{int(NOW * 1000)}-0", NOW)


def test_project_actions_can_be_disabled(op):
    op.ALLOWED_ACTIONS = frozenset({"reject"})
    with pytest.raises(op.Invalid, match="disabled"):
        op.validate({"action": "create_project", "project_id": "web", "name": "W", "source": "empty"},
                    f"{int(NOW * 1000)}-0", NOW)


@pytest.mark.parametrize("request_fields,args", [
    ({"action": "create_project", "project_id": "web", "name": "Web Shop", "importance": "high",
      "source": "empty", "url": "", "gate": "", "push_remote": ""},
     ["create", "--id", "web", "--name", "Web Shop", "--importance", "high", "--empty"]),
    ({"action": "create_project", "project_id": "web", "name": "Web", "importance": "low", "source": "clone",
      "url": "git@github.com:me/web.git", "gate": "npm test", "push_remote": "git@github.com:me/web.git"},
     ["create", "--id", "web", "--name", "Web", "--importance", "low", "--clone", "git@github.com:me/web.git",
      "--gate", "npm test", "--push-remote", "git@github.com:me/web.git"]),
    ({"action": "project_retry_clone", "project_id": "web"}, ["retry-clone", "web"]),
    ({"action": "project_push_setup", "project_id": "web", "url": "git@github.com:me/web.git"},
     ["push-setup", "web", "git@github.com:me/web.git"]),
    ({"action": "delete_project", "project_id": "web", "confirm": "web"}, ["delete", "web", "--confirm", "web"]),
])
def test_project_cli_arguments(op, request_fields, args):
    assert op.project_cli_args(request_fields) == args


def test_project_request_runs_the_cli_and_reports_the_deploy_key(op):
    from types import SimpleNamespace
    seen = []
    runner = lambda cmd, **kw: seen.append(cmd) or SimpleNamespace(
        returncode=0, stdout=json.dumps({"id": "web", "status": "pending_key", "public_key": "ssh-ed25519 AAA"}), stderr="")
    status, message, output = op.execute_project({"action": "project_retry_clone", "project_id": "web"}, runner)
    assert status == "succeeded" and "deploy key created" in message and "ssh-ed25519" in output
    assert seen[0][1].endswith("scripts/sid-project.py") and seen[0][2:] == ["retry-clone", "web"]
    fail = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="ERROR: project id already registered\n")
    assert op.execute_project({"action": "project_retry_clone", "project_id": "web"}, fail)[:2] == (
        "refused", "project id already registered")


def test_project_request_end_to_end_through_the_stream(op, monkeypatch):
    monkeypatch.setattr(op, "execute_project", lambda request: ("succeeded", f"made {request['project_id']}", "{}"))
    op.redis.xadd(STREAM, {"request_id": "req-project1", "action": "create_project", "project_id": "web",
                           "name": "Web", "importance": "high", "source": "empty"})
    deliver(op)
    done = result(op, "req-project1")
    assert done["status"] == "succeeded" and done["message"] == "made web"
    assert done["project_id"] == "web" and done["action"] == "create_project"


def test_delete_request_validates_to_its_confirmation(op):
    request = op.validate({"action": "delete_project", "project_id": "web", "confirm": "web"},
                          f"{int(NOW * 1000)}-0", NOW)
    assert request["confirm"] == "web" and op.project_cli_args(request)[:2] == ["delete", "web"]


def test_commit_upload_request(op):
    request = op.validate({"action": "project_commit_upload", "project_id": "web", "path": "static/a.png",
                           "upload": "upload-0001"}, f"{int(NOW * 1000)}-0", NOW)
    assert op.project_cli_args(request) == ["commit-upload", "web", "--path=static/a.png", "--upload=upload-0001",
                                            "--on-conflict=ask"]
    for bad in ({"path": "/etc/x", "upload": "upload-0001"}, {"path": "a", "upload": "../x"},
                {"path": "a\nb", "upload": "upload-0001"}):
        with pytest.raises(op.Invalid):
            op.validate({"action": "project_commit_upload", "project_id": "web", **bad}, f"{int(NOW * 1000)}-0", NOW)
    with pytest.raises(op.Invalid, match="SID"):
        op.validate({"action": "project_commit_upload", "project_id": "sid", "path": "a", "upload": "upload-0001"},
                    f"{int(NOW * 1000)}-0", NOW)


def test_code_change_request(op):
    request = op.validate({"action": "project_commit_upload", "project_id": "web", "op": "rename",
                           "path": "a.txt", "dest": "b.txt"}, f"{int(NOW * 1000)}-0", NOW)
    assert op.project_cli_args(request) == ["code-change", "web", "--op=rename", "--path=a.txt", "--dest=b.txt"]
    dashed = op.validate({"action": "project_commit_upload", "project_id": "web", "op": "rename", "path": "-x",
                          "dest": "-y"}, f"{int(NOW * 1000)}-0", NOW)
    assert op.project_cli_args(dashed)[-2:] == ["--path=-x", "--dest=-y"]
    with pytest.raises(op.Invalid, match="unknown"):
        op.validate({"action": "project_commit_upload", "project_id": "web", "op": "chmod", "path": "a"},
                    f"{int(NOW * 1000)}-0", NOW)


def test_push_setup_is_refused_for_sid(op):
    with pytest.raises(op.Invalid, match="managed on the host"):
        op.validate({"action": "project_push_setup", "project_id": "sid", "url": "git@github.com:me/x.git"},
                    f"{int(NOW * 1000)}-0", NOW)


def test_batch_requests_are_rechecked(op):
    import json as _json
    good = _json.dumps({"op": "move", "from_area": "data", "to_area": "code", "paths": ["a", "b/c"], "dest": "docs",
                        "resolutions": {"a": "keep"}, "extra": "dropped"})
    request = op.validate({"action": "project_commit_upload", "project_id": "web", "op": "batch", "path": "a",
                           "batch": good}, f"{int(NOW * 1000)}-0", NOW)
    args = op.project_cli_args(request)
    assert args[:2] == ["code-batch", "web"] and "extra" not in args[2] and '"resolutions":{"a":"keep"}' in args[2]
    for bad in ({"op": "chmod", "from_area": "code", "paths": ["a"]}, {"op": "delete", "from_area": "disk", "paths": ["a"]},
                {"op": "delete", "from_area": "code", "paths": []}, {"op": "delete", "from_area": "code", "paths": ["/etc"]},
                {"op": "copy", "from_area": "code", "to_area": "data", "paths": ["a"], "resolutions": {"a": "nuke"}},
                {"op": "delete", "from_area": "code", "paths": ["a"] * 501}):
        with pytest.raises(op.Invalid):
            op.validate({"action": "project_commit_upload", "project_id": "web", "op": "batch", "path": "a",
                         "batch": _json.dumps(bad)}, f"{int(NOW * 1000)}-0", NOW)


def test_restore_request_must_name_that_projects_trash(op):
    stamp = f"{int(NOW * 1000)}-0"
    request = op.validate({"action": "restore_project", "project_id": "shop", "trash_id": "shop-20260930T220000Z"}, stamp, NOW)
    assert op.project_cli_args(request) == ["restore", "shop-20260930T220000Z"]
    for bad in ("other-20260930T220000Z", "shop-../../x", ""):
        with pytest.raises(op.Invalid):
            op.validate({"action": "restore_project", "project_id": "shop", "trash_id": bad}, stamp, NOW)


def test_undo_requests(op):
    stamp = f"{int(NOW * 1000)}-0"
    job = op.validate({"action": "project_revert", "project_id": "shop", "undo_job": "j1"}, stamp, NOW)
    assert op.project_cli_args(job) == ["revert", "shop", "--job=j1"]
    commit = op.validate({"action": "project_revert", "project_id": "shop", "undo_commit": "abc1234"}, stamp, NOW)
    assert op.project_cli_args(commit) == ["revert", "shop", "--commit=abc1234"]
    for bad in ({"undo_job": "j1", "undo_commit": "abc1234"}, {}, {"undo_commit": "--force"}, {"undo_job": "../x"}):
        with pytest.raises(op.Invalid):
            op.validate({"action": "project_revert", "project_id": "shop", **bad}, stamp, NOW)
    with pytest.raises(op.Invalid):
        op.validate({"action": "project_revert", "project_id": "sid", "undo_job": "j1"}, stamp, NOW)
