#!/usr/bin/env python3
"""SID operator service: runs operator actions requested from the Web.

The API has no repository authority. For an operator action it only
records the request in sid:operator-results:<request_id> (status pending)
and appends it to the sid:operator-requests stream. This host-side service
re-validates every request and executes it with the same functions as
scripts/job-review.py, which stays the one authority for Git validation
and merge.

Guarantees:
- A request id runs at most once. The claim is HSETNX on started_at, so a
  redelivered or duplicated stream entry is acknowledged and skipped.
- A request older than OPERATOR_REQUEST_TTL is expired, never executed. Its
  age comes from the stream entry id, i.e. the Redis server clock, not from
  anything the client sent.
- The job must still have the status the human saw (expected_status).
- Approval always carries the exact candidate the human confirmed, and
  job-review.py checks it under the global approval lock.
- A request that was running when the service died is marked interrupted
  and never re-run: re-running a half-done reintegrate could queue a
  second integration. The operator checks the job and submits a new request.
- Actions not in OPERATOR_ALLOWED_ACTIONS are refused. approve is off by
  default while the API is unauthenticated.

job-review.py is loaded once at startup: restart this service after changing it.
"""

import importlib.util
import json
import io
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

from redis import Redis
from redis.exceptions import ResponseError


ROOT = Path(__file__).resolve().parents[2]
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
OPERATOR_ID = os.getenv("OPERATOR_ID", "sid-operator-01")

REQUEST_STREAM = "sid:operator-requests"
CONSUMER_GROUP = "sid-operator"
RESULT_PREFIX = "sid:operator-results:"
HEARTBEAT_PREFIX = "sid:operator-service:"
HEARTBEAT_SECONDS = 10
HEARTBEAT_TTL = 30
READ_BLOCK_MS = 5000
OUTPUT_LIMIT = 8000

ACTIONS = ("approve", "queue_approve", "dequeue_approve", "reject", "extend", "reintegrate", "reopen",
           "create_project", "project_retry_clone", "project_push_setup", "delete_project")
# Project-level actions run scripts/sid-project.py on the host (directories,
# git clone, deploy keys); they carry project fields instead of a job.
PROJECT_ACTIONS = ("create_project", "project_retry_clone", "project_push_setup", "delete_project")
# Actions that carry the exact integrated candidate the human confirmed.
CANDIDATE_ACTIONS = ("approve", "queue_approve")
DEFAULT_ALLOWED_ACTIONS = "reject,extend,reintegrate,reopen,dequeue_approve"
FINAL_STATUSES = {"succeeded", "refused", "error", "expired", "interrupted"}
REQUEST_FIELDS = (
    "request_id", "job_id", "action", "expected_status",
    "expected_candidate", "extra", "requested_from",
    "project_id", "name", "importance", "source", "url", "gate", "confirm",
)
PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
# Web-requested clones and push remotes: network git URLs only (never a host
# path, which would copy an arbitrary local directory into a project).
GIT_URL = re.compile(r"(git@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+|ssh://[^\s]+|https://[^\s]+)")
PROJECT_CLI = ROOT / "scripts/sid-project.py"

JOB_ID = re.compile(r"[A-Za-z0-9]{1,64}")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}")
STATUS = re.compile(r"[a-z_]{1,64}")
FULL_SHA = re.compile(r"[0-9a-f]{40}")


def parse_allowed_actions(raw):
    names = {name.strip() for name in raw.split(",") if name.strip()}
    unknown = names - set(ACTIONS)
    if unknown:
        raise SystemExit(f"OPERATOR_ALLOWED_ACTIONS has unknown actions: {sorted(unknown)}")
    return frozenset(names)


ALLOWED_ACTIONS = parse_allowed_actions(
    os.getenv("OPERATOR_ALLOWED_ACTIONS", DEFAULT_ALLOWED_ACTIONS)
)
REQUEST_TTL = int(os.getenv("OPERATOR_REQUEST_TTL", "600"))

redis = Redis.from_url(REDIS_URL, decode_responses=True)


def load_job_review():
    path = ROOT / "scripts/job-review.py"
    spec = importlib.util.spec_from_file_location("sid_job_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


job_review = None


def log(message):
    # sys.__stdout__: an action's own output is being captured by then.
    print(f"[{OPERATOR_ID}] {message}", file=sys.__stdout__, flush=True)


def result_key(request_id):
    return f"{RESULT_PREFIX}{request_id}"


def heartbeat(status="idle"):
    key = f"{HEARTBEAT_PREFIX}{OPERATOR_ID}"
    redis.hset(key, mapping={
        "id": OPERATOR_ID,
        "status": status,
        "allowed_actions": ",".join(sorted(ALLOWED_ACTIONS)),
        "request_ttl": str(REQUEST_TTL),
        "last_seen": str(time.time()),
    })
    redis.expire(key, HEARTBEAT_TTL)


@contextmanager
def keep_alive(status="working"):
    """Keep the heartbeat fresh while an action runs, so the API does not
    report the service offline (and refuse new requests) mid-action."""
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                heartbeat(status)
            except Exception as exc:
                log(f"heartbeat warning: {exc}")

    thread = threading.Thread(target=beat, name="sid-operator-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=HEARTBEAT_SECONDS + 1)


class Invalid(Exception):
    """The request must not run. status is refused or expired."""

    def __init__(self, message, status="refused"):
        super().__init__(message)
        self.status = status


def entry_age(entry_id, now):
    """Seconds since Redis accepted the stream entry (id is <ms>-<seq>)."""
    try:
        return now - int(entry_id.split("-", 1)[0]) / 1000
    except (ValueError, AttributeError):
        raise Invalid("unreadable stream entry id")


def validate_project_request(action, fields):
    project_id = fields.get("project_id", "")
    if not PROJECT_ID.fullmatch(project_id):
        raise Invalid("invalid project id")
    request = {"action": action, "project_id": project_id, "job_id": ""}
    url = fields.get("url", "")
    if action == "create_project":
        name = fields.get("name", "").strip()
        if not name or len(name) > 80 or not name.isprintable():
            raise Invalid("project name must be 1-80 printable characters")
        importance = fields.get("importance", "medium")
        if importance not in ("high", "medium", "low"):
            raise Invalid("importance must be high, medium or low")
        source = fields.get("source", "")
        if source not in ("empty", "clone"):
            raise Invalid("source must be empty or clone")
        if source == "clone" and not GIT_URL.fullmatch(url):
            raise Invalid("clone needs a git@..., ssh:// or https:// URL")
        push_remote = fields.get("push_remote", "")
        if push_remote and not GIT_URL.fullmatch(push_remote):
            raise Invalid("push remote must be a git@..., ssh:// or https:// URL")
        gate = fields.get("gate", "")
        if len(gate) > 200 or "\n" in gate or "\r" in gate:
            raise Invalid("gate command must be one line of at most 200 characters")
        request.update(name=name, importance=importance, source=source, url=url,
                       push_remote=push_remote, gate=gate)
    elif action == "delete_project":
        if project_id == "sid":
            raise Invalid("SID itself cannot be deleted")
        if fields.get("confirm", "") != project_id:
            raise Invalid("type the project id to confirm deletion")
        request["confirm"] = project_id
    elif action == "project_push_setup":
        if not GIT_URL.fullmatch(url):
            raise Invalid("push setup needs a git@..., ssh:// or https:// URL")
        request["url"] = url
    return request


def project_cli_args(request):
    action, project_id = request["action"], request["project_id"]
    if action == "create_project":
        args = ["create", "--id", project_id, "--name", request["name"],
                "--importance", request["importance"]]
        args += ["--empty"] if request["source"] == "empty" else ["--clone", request["url"]]
        if request.get("gate"):
            args += ["--gate", request["gate"]]
        if request.get("push_remote"):
            args += ["--push-remote", request["push_remote"]]
        return args
    if action == "project_retry_clone":
        return ["retry-clone", project_id]
    if action == "delete_project":
        return ["delete", project_id, "--confirm", request["confirm"]]
    return ["push-setup", project_id, request["url"]]


def execute_project(request, runner=subprocess.run):
    """Run sid-project.py on the host. Returns (status, message, output)."""
    try:
        result = runner(["/usr/bin/python3", str(PROJECT_CLI), *project_cli_args(request)],
                        text=True, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        return "error", "project command timed out after 300s", ""
    if result.returncode != 0:
        reason = (result.stderr.strip().splitlines() or ["project command failed"])[-1]
        return "refused", reason.removeprefix("ERROR: "), result.stderr[-OUTPUT_LIMIT:]
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = {}
    summary = f"{request['action']} {request['project_id']}: {data.get('status', 'done')}"
    if data.get("public_key"):
        summary += " (deploy key created: add it to the GitHub repository with write access)"
    return "succeeded", summary, result.stdout[-OUTPUT_LIMIT:]


def validate(fields, entry_id, now):
    """Re-check everything the API checked; never trust the stream."""
    action = fields.get("action", "")
    if action in PROJECT_ACTIONS:
        if action not in ALLOWED_ACTIONS:
            raise Invalid(f"action {action!r} is disabled on this host (OPERATOR_ALLOWED_ACTIONS)")
        age = entry_age(entry_id, now)
        if age > REQUEST_TTL:
            raise Invalid(f"request expired: made {int(age)}s ago, limit {REQUEST_TTL}s",
                          status="expired")
        return validate_project_request(action, fields)
    job_id = fields.get("job_id", "")
    expected_status = fields.get("expected_status", "")
    candidate = fields.get("expected_candidate", "")
    extra = fields.get("extra", "")

    if action not in ACTIONS:
        raise Invalid(f"unknown action {action!r}")
    if action not in ALLOWED_ACTIONS:
        raise Invalid(
            f"action {action!r} is disabled on this host "
            "(OPERATOR_ALLOWED_ACTIONS); use scripts/job-review.py"
        )
    if not JOB_ID.fullmatch(job_id):
        raise Invalid("invalid job id")
    if not STATUS.fullmatch(expected_status):
        raise Invalid("expected_status is required")
    if action in CANDIDATE_ACTIONS:
        if not FULL_SHA.fullmatch(candidate):
            raise Invalid(
                "approve requires the full 40-character candidate SHA the human confirmed"
            )
    elif candidate:
        raise Invalid(f"expected_candidate is only accepted for approve/queue_approve, not {action}")
    if action == "extend":
        if not (extra.isdigit() and 1 <= int(extra) <= 5):
            raise Invalid("extend requires extra between 1 and 5")
    elif extra:
        raise Invalid(f"extra is only accepted for extend, not {action}")

    age = entry_age(entry_id, now)
    if age > REQUEST_TTL:
        raise Invalid(
            f"request expired: made {int(age)}s ago, limit {REQUEST_TTL}s; "
            "refresh and decide again",
            status="expired",
        )

    return {
        "action": action,
        "job_id": job_id,
        "expected_status": expected_status,
        "expected_candidate": candidate or None,
        "extra": int(extra) if extra else None,
    }


def call_action(request):
    action, job_id = request["action"], request["job_id"]
    if action == "approve":
        job_review.approve(job_id, expected_candidate=request["expected_candidate"])
    elif action == "queue_approve":
        job_review.queue_approval(job_id, request["expected_candidate"])
    elif action == "dequeue_approve":
        job_review.dequeue_approval(job_id)
    elif action == "reject":
        job_review.reject(job_id)
    elif action == "extend":
        job_review.extend(job_id, request["extra"])
    elif action == "reintegrate":
        job_review.reintegrate(job_id)
    else:
        job_review.reopen(job_id)


def execute(request):
    """Run one validated request. Returns (status, message, output)."""
    if request["action"] in PROJECT_ACTIONS:
        return execute_project(request)
    job_status = redis.hget(f"sid:jobs:{request['job_id']}", "status")
    if job_status is None:
        return "refused", f"job not found: {request['job_id']}", ""
    if job_status != request["expected_status"]:
        return "refused", (
            f"job status is {job_status!r}, but the request was made when it "
            f"was {request['expected_status']!r}; refresh and decide again"
        ), ""

    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            call_action(request)
    except job_review.Refused as exc:
        status, message = "refused", exc.message
    except SystemExit as exc:
        status, message = "error", f"action exited with code {exc.code}"
    except Exception as exc:
        status, message = "error", f"{type(exc).__name__}: {exc}"
    else:
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        status, message = "succeeded", lines[-1] if lines else "done"
    output = (out.getvalue() + err.getvalue())[-OUTPUT_LIMIT:]
    return status, message, output


def finish(key, status, message, output=""):
    redis.hset(key, mapping={
        "status": status,
        "message": message,
        "output": output,
        "finished_at": str(time.time()),
    })


def handle(entry_id, fields, now):
    request_id = fields.get("request_id", "")
    if not REQUEST_ID.fullmatch(request_id):
        log(f"dropping stream entry {entry_id}: invalid request id")
        return
    key = result_key(request_id)
    # The claim. Anything already started, by us or a previous run, is
    # never executed again.
    if not redis.hsetnx(key, "started_at", str(now)):
        log(f"request {request_id} already handled; skipping entry {entry_id}")
        return
    for field in REQUEST_FIELDS:
        redis.hsetnx(key, field, fields.get(field, ""))
    redis.hset(key, mapping={
        "status": "running", "service_id": OPERATOR_ID, "stream_id": entry_id,
    })
    try:
        request = validate(fields, entry_id, now)
    except Invalid as exc:
        finish(key, exc.status, str(exc))
        log(f"request {request_id}: {exc.status}: {exc}")
        return
    with keep_alive():
        status, message, output = execute(request)
    finish(key, status, message, output)
    log(f"request {request_id}: {request['action']} {request['job_id']}: {status}: {message}")


def process(entry_id, fields, now=None):
    handle(entry_id, fields, time.time() if now is None else now)
    # Acknowledge only after the result is recorded. If handle() raised
    # (Redis failure), the entry stays pending and recover() resolves it.
    redis.xack(REQUEST_STREAM, CONSUMER_GROUP, entry_id)


def run_merge_queue():
    """Merge the next fresh queued approval (job-review.py merge queue)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            merged = job_review.process_merge_queue()
    except SystemExit as exc:  # Refused from git plumbing, not from a merge
        log(f"merge queue: {getattr(exc, 'message', exc)}")
        return None
    except Exception as exc:
        log(f"merge queue error: {type(exc).__name__}: {exc}")
        return None
    for line in (out.getvalue() + err.getvalue()).splitlines():
        if line.strip():
            log(f"merge queue: {line}")
    return merged


def ensure_group():
    try:
        redis.xgroup_create(REQUEST_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def recover(now=None):
    """Resolve entries delivered to this consumer but never acknowledged."""
    now = time.time() if now is None else now
    replies = redis.xreadgroup(
        CONSUMER_GROUP, OPERATOR_ID, {REQUEST_STREAM: "0"}, count=1000,
    )
    for _stream, messages in replies or []:
        for entry_id, fields in messages:
            if not fields:  # entry trimmed from the stream
                redis.xack(REQUEST_STREAM, CONSUMER_GROUP, entry_id)
                continue
            key = result_key(fields.get("request_id", ""))
            result = redis.hgetall(key)
            if result.get("started_at") and result.get("status") not in FINAL_STATUSES:
                finish(key, "interrupted",
                       "the operator service stopped while this request was "
                       "running; check the job state before submitting a new request")
                log(f"request {fields.get('request_id')}: marked interrupted")
                redis.xack(REQUEST_STREAM, CONSUMER_GROUP, entry_id)
            else:
                process(entry_id, fields, now)


def main():
    global job_review
    job_review = load_job_review()
    log(f"SID operator starting; allowed actions: {sorted(ALLOWED_ACTIONS)}; "
        f"request ttl {REQUEST_TTL}s")
    ensure_group()
    recover()
    while True:
        try:
            heartbeat()
            replies = redis.xreadgroup(
                CONSUMER_GROUP, OPERATOR_ID, {REQUEST_STREAM: ">"},
                count=1, block=READ_BLOCK_MS,
            )
            for _stream, messages in replies or []:
                for entry_id, fields in messages:
                    heartbeat("working")
                    process(entry_id, fields)
            run_merge_queue()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            log(f"operator error: {exc}")
            time.sleep(5)
            # A Redis outage may have left an entry unacknowledged.
            try:
                recover()
            except Exception as recover_exc:
                log(f"recovery failed: {recover_exc}")


if __name__ == "__main__":
    main()
