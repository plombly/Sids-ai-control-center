#!/usr/bin/env python3

import json
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import redis


REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
GOAL_QUEUE = os.getenv("GOAL_QUEUE", "sid:goals")
JOB_QUEUE = os.getenv("WORKER_QUEUE", "sid:jobs")
REPO_ROOT = Path(
    os.getenv("REPO_ROOT", "/opt/sids-ai-command-center")
).resolve()
DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "codex")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-5.6-luna")
ORCHESTRATOR_ID = os.getenv("ORCHESTRATOR_ID", "sid-orchestrator-01")
PLAN_TIMEOUT = int(os.getenv("PLAN_TIMEOUT", "180"))
MAX_REPAIR_ATTEMPTS = int(
    os.getenv("MAX_REPAIR_ATTEMPTS", "2")
)
MAX_CONCURRENT_GOALS = max(1, int(os.getenv("MAX_CONCURRENT_GOALS", "4")))
# Self-healing bounds. A failed build is rebuilt from current main until
# MAX_BUILD_ATTEMPTS, and a review that could not complete is re-integrated
# and re-reviewed up to MAX_REVIEW_RECOVERIES times. Past either bound the
# job goes to needs_human (not a failure), so dependents wait for a person.
MAX_BUILD_ATTEMPTS = int(os.getenv("MAX_BUILD_ATTEMPTS", "2"))
MAX_REVIEW_RECOVERIES = int(os.getenv("MAX_REVIEW_RECOVERIES", "2"))
# Work neither queued nor held by a live worker must stay that way this long
# (across loop passes) before it is declared lost.
LOST_CONFIRM_SECONDS = int(os.getenv("LOST_JOB_CONFIRM_SECONDS", "60"))

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def now():
    return str(time.time())


def heartbeat(status="idle", goal_id="", goal_ids=None):
    if goal_id and status == "idle":
        status = "active"
    r.hset(
        f"sid:orchestrators:{ORCHESTRATOR_ID}",
        mapping={
            "id": ORCHESTRATOR_ID,
            "status": status,
            "goal_id": goal_id,
            "goal_ids": json.dumps(goal_ids or ([goal_id] if goal_id else [])),
            "model": DEFAULT_MODEL,
            "last_seen": now(),
        },
    )
    r.expire(f"sid:orchestrators:{ORCHESTRATOR_ID}", 30)


def extract_json(text):
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError("planner returned no JSON object")

    return json.loads(text[start : end + 1])



def repository_manifest():
    try:
        out = subprocess.check_output(
            ["git", "ls-files"], cwd=REPO_ROOT, text=True, timeout=10
        )
        files = [line for line in out.splitlines() if line.strip()]
        shown = files[:160]
        suffix = f"\n... {len(files) - len(shown)} more files" if len(files) > len(shown) else ""
        return "\n".join(shown) + suffix
    except Exception:
        return "manifest unavailable"

def planner_prompt(goal, atomic=False):
    return f"""
You are the planning agent for SID's AI Command Center.

Repository:
{REPO_ROOT}

High-level goal:
{goal}

ATOMIC MODE: {"ENABLED" if atomic else "disabled"}

Plan from the goal and the compact repository manifest below. Do not broadly inspect the repository unless a specific ambiguity prevents a safe plan.

Repository manifest:
{repository_manifest()}

Break the goal into a SMALL set of implementation jobs that can be executed
by independent coding agents.

Rules:
- Do not modify files.
- If ATOMIC MODE is enabled, return exactly one implementation job with no dependencies.
- Without atomic mode, use one job for a narrow independently-testable change; split only when there is a concrete dependency or separable ownership boundary.
- Prefer 1-5 coherent jobs.
- Avoid microscopic jobs.
- Minimize overlapping file ownership between parallel jobs.
- Dependencies must reference job numbers from this plan.
- Every job must be independently testable.
- Existing SID review and human approval gates will handle merging.
- Do not include deployment or Git merge jobs.
- Do not include a reviewer job; SID creates reviews automatically.
- Be conservative about architecture changes.
- Preserve existing functionality.

Return ONLY valid JSON using this exact shape:

{{
  "summary": "short plan summary",
  "jobs": [
    {{
      "number": 1,
      "title": "short title",
      "task": "complete implementation instructions",
      "scope": ["likely/relevant/path"],
      "depends_on": []
    }}
  ]
}}
""".strip()


def run_planner(goal, atomic=False):
    cmd = [
        "codex",
        "exec",
        "--model",
        DEFAULT_MODEL,
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--color",
        "never",
        "--json",
        "--cd",
        str(REPO_ROOT),
        "-",
    ]

    proc = subprocess.run(
        cmd,
        input=planner_prompt(goal, atomic=atomic),
        text=True,
        capture_output=True,
        timeout=PLAN_TIMEOUT,
        env={**os.environ, "HOME": "/root"},
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"planner failed ({proc.returncode}): {proc.stderr[-2000:]}"
        )

    messages = []

    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") != "item.completed":
            continue

        item = event.get("item") or {}

        if item.get("type") == "agent_message":
            text = item.get("text")
            if text:
                messages.append(text)

    if not messages:
        raise RuntimeError("planner produced no final agent message")

    return extract_json(messages[-1])


def validate_plan(plan, atomic=False):
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")

    jobs = plan.get("jobs")

    if not isinstance(jobs, list) or not jobs:
        raise ValueError("plan contains no jobs")

    if len(jobs) > 10:
        raise ValueError("planner produced too many jobs")

    if atomic and len(jobs) != 1:
        raise ValueError(f"atomic goal requires exactly one job; planner returned {len(jobs)}")

    numbers = set()

    for job in jobs:
        number = job.get("number")

        if not isinstance(number, int) or number < 1:
            raise ValueError("invalid job number")

        if number in numbers:
            raise ValueError("duplicate job number")

        numbers.add(number)

        if not str(job.get("title", "")).strip():
            raise ValueError(f"job {number} has no title")

        if not str(job.get("task", "")).strip():
            raise ValueError(f"job {number} has no task")

        scope = job.get("scope", [])
        if not isinstance(scope, list) or any(not isinstance(x, str) for x in scope):
            raise ValueError(f"job {number} scope must be a list of paths")
        if len(scope) > 12:
            raise ValueError(f"job {number} scope is too broad")

        deps = job.get("depends_on", [])

        if not isinstance(deps, list):
            raise ValueError(f"job {number} dependencies must be a list")

    for job in jobs:
        number = job["number"]

        for dep in job.get("depends_on", []):
            if dep not in numbers:
                raise ValueError(
                    f"job {number} references unknown dependency {dep}"
                )

            if dep >= number:
                raise ValueError(
                    f"job {number} dependency {dep} must precede it"
                )

    return jobs


def create_job(goal_id, planned_job, number_to_id):
    job_id = uuid.uuid4().hex[:8]
    dependencies = [
        number_to_id[n]
        for n in planned_job.get("depends_on", [])
    ]

    record = {
        "id": job_id,
        "goal_id": goal_id,
        "title": planned_job["title"],
        "prompt": planned_job["task"],
        "provider": DEFAULT_PROVIDER,
        "model": DEFAULT_MODEL,
        "role": "builder",
        "priority": "0",
        "dependencies": json.dumps(dependencies),
        "status": "blocked" if dependencies else "queued",
        "build_attempt": "1",
        "created_at": now(),
        "updated_at": now(),
    }

    r.hset(f"sid:jobs:{job_id}", mapping=record)

    if not dependencies:
        dispatch_job_once(record, {
            "id": job_id,
            "goal_id": goal_id,
            "prompt": planned_job["task"],
            "provider": DEFAULT_PROVIDER,
            "model": DEFAULT_MODEL,
            "role": "builder",
            "priority": 0,
            "created_at": record["created_at"],
        })

    return job_id


def dispatch_job_once(job, payload):
    """Queue a job only once, including when scheduling is retried."""
    key = f"sid:jobs:{job['id']}"
    if not r.hsetnx(key, "dispatch_reserved_at", now()):
        return False
    try:
        r.rpush(JOB_QUEUE, json.dumps(payload))
    except Exception:
        r.hdel(key, "dispatch_reserved_at")
        raise
    return True



CONTEXT_FILE_LIMIT = int(os.getenv("CONTEXT_FILE_LIMIT", "4"))
CONTEXT_FILE_CHARS = int(os.getenv("CONTEXT_FILE_CHARS", "3000"))
CONTEXT_TOTAL_CHARS = int(os.getenv("CONTEXT_TOTAL_CHARS", "6000"))


def scoped_context_packet(item):
    """Build a deterministic, bounded starting context from planner scope."""
    scope = [p.strip() for p in item.get("scope", []) if p.strip()][:12]
    chunks = []
    used = 0
    for raw in scope:
        path = (REPO_ROOT / raw).resolve()
        try:
            path.relative_to(REPO_ROOT)
        except ValueError:
            continue
        # V3 deliberately embeds only exact files. A directory scope is a hint,
        # not permission to dump arbitrary alphabetical files into the prompt.
        candidates = [path] if path.is_file() else []
        for candidate in candidates:
            if len(chunks) >= CONTEXT_FILE_LIMIT or used >= CONTEXT_TOTAL_CHARS:
                break
            try:
                text = candidate.read_text(errors="replace")
            except OSError:
                continue
            remaining = CONTEXT_TOTAL_CHARS - used
            body = text[:min(CONTEXT_FILE_CHARS, remaining)]
            rel = candidate.relative_to(REPO_ROOT)
            chunks.append(f"--- {rel} ---\n{body}")
            used += len(body)
        if len(chunks) >= CONTEXT_FILE_LIMIT or used >= CONTEXT_TOTAL_CHARS:
            break
    return "\n\n".join(chunks) or "(No scoped file content available; inspect only the likely scope below.)"


def scoped_builder_prompt(item):
    scope = [p.strip() for p in item.get("scope", []) if p.strip()]
    scope_text = "\n".join(f"- {p}" for p in scope) or "- infer the smallest relevant scope"
    context = scoped_context_packet(item)
    return f"""Task:
{item['task']}

Likely scope (start here; expand only if required):
{scope_text}

SID context packet (bounded starting context; trust repository files over this snapshot if you edit them):
{context}

Efficiency requirements:
- Start from the context packet and likely scope; do not rediscover information already provided.
- Do not inventory or read the whole repository.
- Expand beyond likely scope only for a concrete dependency required by the task.
- Make the smallest correct change.
- Run only focused validation; SID runs the deterministic integration gate.
- Stop when the requested implementation and focused validation are complete.
""".strip()

def process_goal(raw):
    data = json.loads(raw)
    goal_id = data["id"]
    goal = data["goal"]
    key = f"sid:goals:{goal_id}"

    # Queue delivery is at-least-once. A short-lived claim prevents a retry or
    # a second orchestrator from creating a second plan for the same goal.
    claim_key = f"{key}:planning"
    if not r.set(claim_key, ORCHESTRATOR_ID, nx=True, ex=PLAN_TIMEOUT + 30):
        return

    existing_status = r.hget(key, "status")
    if existing_status in {
        "planning", "running", "completed", "failed", "planning_failed",
    }:
        r.delete(claim_key)
        return

    heartbeat("planning", goal_id)

    r.hset(
        key,
        mapping={
            "status": "planning",
            "updated_at": now(),
            "orchestrator": ORCHESTRATOR_ID,
        },
    )

    atomic = str(data.get("atomic", "")).lower() in {"1", "true", "yes"}
    plan = run_planner(goal, atomic=atomic)
    jobs = validate_plan(plan, atomic=atomic)

    number_to_id = {}

    # IDs first so dependencies can be resolved deterministically.
    for item in jobs:
        number_to_id[item["number"]] = uuid.uuid4().hex[:8]

    job_ids = []

    for item in jobs:
        job_id = number_to_id[item["number"]]
        dependencies = [
            number_to_id[n]
            for n in item.get("depends_on", [])
        ]

        record = {
            "id": job_id,
            "goal_id": goal_id,
            "title": item["title"],
            "prompt": scoped_builder_prompt(item),
            "provider": DEFAULT_PROVIDER,
            "model": DEFAULT_MODEL,
            "role": "builder",
            "priority": "0",
            "dependencies": json.dumps(dependencies),
            "status": "blocked" if dependencies else "queued",
            "created_at": now(),
            "updated_at": now(),
            "prompt_chars": str(len(scoped_builder_prompt(item))),
            "scope": json.dumps(item.get("scope", [])),
        }

        r.hset(f"sid:jobs:{job_id}", mapping=record)

        if not dependencies:
            dispatch_job_once(record, {
                    "id": job_id,
                    "goal_id": goal_id,
                    "prompt": scoped_builder_prompt(item),
                    "provider": DEFAULT_PROVIDER,
                    "model": DEFAULT_MODEL,
                    "role": "builder",
                    "priority": 0,
                    "created_at": record["created_at"],
                })

        job_ids.append(job_id)

    r.hset(
        key,
        mapping={
            "status": "running",
            "plan": json.dumps(plan),
            "jobs": json.dumps(job_ids),
            "updated_at": now(),
        },
    )

    print(
        f"[{ORCHESTRATOR_ID}] goal={goal_id} planned "
        f"{len(job_ids)} jobs",
        flush=True,
    )
    r.delete(claim_key)



def review_findings(review):
    log_value = review.get("log", "")
    if not log_value:
        return "Reviewer requested changes but supplied no readable log."

    path = Path(log_value)

    if not path.exists():
        return "Reviewer requested changes; review log is unavailable."

    messages = []

    try:
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") != "item.completed":
                continue

            item = event.get("item") or {}

            if item.get("type") == "agent_message":
                text = str(item.get("text", "")).strip()
                if text:
                    messages.append(text)
    except OSError as exc:
        return f"Reviewer requested changes; log read failed: {exc}"

    if not messages:
        return "Reviewer requested changes but emitted no findings."

    # Keep the repair prompt focused. The final reviewer messages contain
    # the actionable findings and verdict; do not replay the full session.
    findings = "\n\n".join(messages[-2:])
    return findings[-6000:]


def queue_repairs():
    for key in r.scan_iter("sid:jobs:*"):
        builder = r.hgetall(key)

        if builder.get("status") != "awaiting_review":
            continue

        if builder.get("review_status") != "complete":
            continue

        if builder.get("review_verdict") != "changes_required":
            continue

        if builder.get("role", "builder") not in {"", "builder"}:
            continue

        attempts = int(builder.get("repair_attempts", "0") or "0")
        # A human may grant more attempts per job (scripts/job-review.py
        # extend); otherwise the global default applies.
        try:
            repair_limit = int(builder.get("max_repair_attempts") or MAX_REPAIR_ATTEMPTS)
        except ValueError:
            repair_limit = MAX_REPAIR_ATTEMPTS

        # A dispatched repair must be allowed to finish before deciding that
        # the builder has exhausted its repair allowance. In particular, the
        # final allowed attempt sets repair_attempts == MAX_REPAIR_ATTEMPTS
        # while that repair is still in flight.
        existing = builder.get("repair_job_id")
        if existing:
            repair = r.hgetall(f"sid:jobs:{existing}")
            if repair.get("status") in {
                "queued",
                "claimed",
                "repairing",
                "testing",
            }:
                continue

        if attempts >= repair_limit:
            # Exhaustion hands the job to a human instead of failing it.
            # "needs_human" is not a failure state, so dependents keep
            # waiting and the goal stays open. The operator can extend
            # repairs, reintegrate for a fresh review, or reject. Written
            # once: the job leaves "awaiting_review", so this loop no longer
            # selects it and updated_at does not churn.
            r.hset(
                key,
                mapping={
                    "status": "needs_human",
                    "repair_status": "exhausted",
                    "needs_human_reason": (
                        f"review still requires changes after {attempts} "
                        "repair attempt(s)"
                    ),
                    "updated_at": now(),
                },
            )
            continue

        if existing:
            # The active-repair reservation must not survive a completed
            # repair. Preserve the historical repair ID separately so a
            # later CHANGES_REQUIRED verdict can reserve the next attempt.
            r.hset(key, "last_repair_job_id", existing)
            r.hdel(key, "repair_job_id")
            builder.pop("repair_job_id", None)

        review_job_id = builder.get("review_job_id")
        if not review_job_id:
            continue

        review = r.hgetall(f"sid:jobs:{review_job_id}")

        if not review:
            continue

        findings = review_findings(review)
        repair_job_id = uuid.uuid4().hex[:8]
        next_attempt = attempts + 1

        # Atomic reservation prevents the orchestrator loop from
        # dispatching the same repair twice.
        if not r.hsetnx(key, "repair_job_id", repair_job_id):
            continue

        prompt = f"""You are a focused repair agent for SID's AI Command Center.

Builder job:
{builder.get("id", "")}

Original task:
{builder.get("prompt", "")}

The immutable candidate was independently reviewed and changes were required.

Reviewer findings:
{findings}

Repair attempt:
{next_attempt} of {repair_limit}

Work ONLY on the concrete reviewer findings necessary to satisfy the original
task. Preserve correct existing work. Do not broaden the scope, redesign
unrelated code, merge branches, or commit changes yourself.

Inspect the existing candidate first. Make the smallest correct repair.
Validate the affected behavior. SID will run its deterministic test gate
after you finish.
"""

        created = now()

        payload = {
            "id": repair_job_id,
            "prompt": prompt,
            "provider": builder.get(
                "provider",
                DEFAULT_PROVIDER,
            ),
            "model": builder.get(
                "model",
                DEFAULT_MODEL,
            ),
            "role": "repair",
            "target_builder_id": builder.get("id", ""),
            "worktree": builder.get("worktree", ""),
            "created_at": created,
        }

        try:
            r.hset(
                f"sid:jobs:{repair_job_id}",
                mapping={
                    "id": repair_job_id,
                    "status": "queued",
                    "role": "repair",
                    "provider": payload["provider"],
                    "model": payload["model"],
                    "target_builder_id": payload[
                        "target_builder_id"
                    ],
                    "worktree": payload["worktree"],
                    "prompt": prompt,
                    "repair_attempt": str(next_attempt),
                    "created_at": created,
                    "updated_at": created,
                },
            )

            r.hset(
                key,
                mapping={
                    "repair_attempts": str(next_attempt),
                    "repair_status": "queued",
                    "updated_at": now(),
                },
            )

            r.rpush(JOB_QUEUE, json.dumps(payload))

            print(
                f"[{ORCHESTRATOR_ID}] repair={repair_job_id} "
                f"builder={builder.get('id')} "
                f"attempt={next_attempt}/{repair_limit}",
                flush=True,
            )

        except Exception:
            current = r.hget(key, "repair_job_id")

            if current == repair_job_id:
                r.hdel(key, "repair_job_id", "repair_status")

            r.delete(f"sid:jobs:{repair_job_id}")
            raise

def release_dependencies():
    for key in r.scan_iter("sid:jobs:*"):
        job = r.hgetall(key)

        if job.get("status") != "blocked":
            continue

        deps = json.loads(job.get("dependencies", "[]"))

        if not deps:
            continue

        dep_jobs = [r.hgetall(f"sid:jobs:{dep}") for dep in deps]
        dep_states = [dep.get("status") for dep in dep_jobs]

        # blocked_failed_dependency is itself a failure for grandchildren;
        # without it, jobs two levels below a failure stay "blocked" forever.
        # A failed build that will be retried is not terminal yet.
        if any(is_terminal_failure(dep) for dep in dep_jobs):
            r.hset(
                key,
                mapping={
                    "status": "blocked_failed_dependency",
                    "updated_at": now(),
                },
            )
            continue

        # A dependency is complete only after it is merged, or when
        # it correctly determined that no changes were required.
        if not all(
            state in {"merged", "completed_no_changes"}
            for state in dep_states
        ):
            continue

        job_id = job["id"]

        payload = {
            "id": job_id,
            "goal_id": job.get("goal_id", ""),
            "prompt": job["prompt"],
            "provider": job.get("provider", DEFAULT_PROVIDER),
            "model": job.get("model", DEFAULT_MODEL),
            "role": "builder",
            "priority": int(job.get("priority", "0")),
            "created_at": job.get("created_at", now()),
        }

        # Reserve before enqueueing so repeated scans cannot dispatch twice.
        if dispatch_job_once(job, payload):
            r.hset(
                key,
                mapping={
                    "status": "queued",
                    "updated_at": now(),
                },
            )

            print(
                f"[{ORCHESTRATOR_ID}] released job={job_id}",
                flush=True,
            )


TERMINAL_FAILURE_STATES = {
    "failed", "test_failed", "integration_failed", "rejected",
    "repair_exhausted", "blocked_failed_dependency",
}
BUILD_FAILED_STATES = {"failed", "test_failed", "integration_failed"}
IN_FLIGHT_STATES = {
    "queued", "claimed", "running", "testing",
    "reviewing", "repairing", "integrating",
}
# Same set scripts/job-review.py reintegrate clears.
DERIVED_REVIEW_FIELDS = (
    "review_job_id", "review_status", "review_verdict", "reviewed_commit",
    "review_error", "review_findings",
    "integration_status", "integration_base_commit",
    "integrated_candidate_commit", "integration_result", "integration_error",
)
# A rebuild starts from main: nothing derived from the previous attempt's
# candidate survives. Findings history and audit fields are kept.
BUILD_DERIVED_FIELDS = DERIVED_REVIEW_FIELDS + (
    "candidate_commit", "source_candidate_commits", "changes",
    "files_changed", "test_status", "error", "codex_exit_code",
    "dispatch_reserved_at", "repair_job_id", "last_repair_job_id",
    "repair_status", "repair_error", "review_history",
    "last_integrate_job_id", "review_recoveries",
    "needs_human_reason", "needs_human_kind", "failed_status",
)

# Loop-local memory of work that looked lost: job/builder id -> first seen.
_lost_suspects = {}


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_builder(job):
    return job.get("role", "builder") in {"", "builder"}


def build_limit(job):
    return _int(job.get("max_build_attempts"), MAX_BUILD_ATTEMPTS)


def build_retry_pending(job):
    """A failed build that retry_failed_builds() will rebuild. Records from
    before retries existed have no build_attempt and are never retried."""
    return (
        is_builder(job)
        and job.get("status") in BUILD_FAILED_STATES
        and bool(job.get("build_attempt"))
        and _int(job.get("build_attempt"), 1) < build_limit(job)
    )


def is_terminal_failure(job):
    return (
        job.get("status") in TERMINAL_FAILURE_STATES
        and not build_retry_pending(job)
    )


def live_work():
    """(ids held by live workers, queued ids), or None if any live worker
    runs code too old to report the job it holds."""
    held = set()
    for key in r.scan_iter("sid:workers:*"):
        beat = r.hgetall(key)
        if not beat:
            continue
        if "job_id" not in beat:
            return None
        if beat["job_id"]:
            held.add(beat["job_id"])
    queued = set()
    for raw in r.lrange(JOB_QUEUE, 0, -1):
        try:
            queued.add(str(json.loads(raw).get("id", "")))
        except (ValueError, AttributeError):
            continue
    return held, queued


def confirmed_lost(suspect, seen, now_ts):
    """True once `suspect` has looked lost for LOST_CONFIRM_SECONDS."""
    seen.add(suspect)
    first = _lost_suspects.setdefault(suspect, now_ts)
    return now_ts - first >= LOST_CONFIRM_SECONDS


def release_stale_integration_lock(builder_id):
    """Drop a builder's integration lock whose holder is gone. Only called
    when no live worker holds any job for this builder."""
    lock_key = f"sid:integration-lock:{builder_id}"
    owner = r.get(lock_key)
    if owner:
        r.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1, lock_key, owner,
        )


def test_output_excerpt(output, limit=3000):
    """The part of SID test output that explains a failure.

    pytest -q prints failure details first and the warnings summary last, so
    the tail of the log is usually only deprecation warnings. Prefer the
    FAILURES/ERRORS section and the short summary; otherwise drop warnings.
    """
    def section(start_marker, end_markers):
        start = output.find(start_marker)
        if start < 0:
            return ""
        ends = [output.find(m, start + len(start_marker)) for m in end_markers]
        ends = [e for e in ends if e >= 0]
        return output[start:min(ends) if ends else len(output)].strip()

    ends = ("warnings summary", "short test summary info")
    details = section("= FAILURES =", ends) or section("= ERRORS =", ends)
    summary = section("short test summary info", ("\n$ ",))
    if details or summary:
        return (details[:limit - 800] + "\n...\n" + summary[-800:]).strip()
    kept, skipping = [], False
    for line in output.splitlines():
        if "warnings summary" in line:
            skipping = True
        elif skipping and (line.startswith("-- Docs:") or line.startswith("$ ")):
            skipping = line.startswith("-- Docs:")
            if skipping:
                skipping = False
                continue
        if not skipping:
            kept.append(line)
    return "\n".join(kept)[-limit:]


def failure_reason(job):
    status = job.get("status")
    if status == "test_failed":
        output = ""
        try:
            output = Path(job.get("test_log", "")).read_text()
        except OSError:
            pass
        return "SID test gate failed:\n" + (test_output_excerpt(output) or "(no test log)")
    if status == "integration_failed":
        reason = "integration onto main failed: " + (job.get("integration_error") or "unknown")
        try:
            gate = json.loads(job.get("integration_result") or "{}")
        except ValueError:
            gate = {}
        stdout = gate.get("stdout", "") if isinstance(gate, dict) else ""
        if "[ FAIL ]" in stdout:
            reason += "\n" + stdout[stdout.index("[ FAIL ]"):][-2500:]
        return reason
    return job.get("error") or "unknown error"


def recover_lost_jobs(now_ts=None):
    """Fail jobs that look in flight but no live worker holds and the queue
    does not contain: their worker died (restart, crash, reboot). The retry
    and stall recovery below then carry the work forward."""
    work = live_work()
    if work is None:
        return
    held, queued = work
    now_ts = time.time() if now_ts is None else now_ts
    seen = set()
    for key in r.scan_iter("sid:jobs:*"):
        job = r.hgetall(key)
        job_id = job.get("id") or key.rsplit(":", 1)[-1]
        if job.get("status") not in IN_FLIGHT_STATES:
            continue
        if job_id in held or job_id in queued:
            continue
        if not confirmed_lost(f"job:{job_id}", seen, now_ts):
            continue
        r.hset(key, mapping={
            "status": "failed",
            "error": (
                f"worker lost: job was {job.get('status')!r} but no live worker "
                f"held it and it was not queued (last worker "
                f"{job.get('worker_id') or 'none'})"
            ),
            "lost_at": now(),
            "updated_at": now(),
        })
        print(f"[{ORCHESTRATOR_ID}] lost job={job_id} marked failed", flush=True)
    for suspect in [s for s in _lost_suspects if s.startswith("job:") and s not in seen]:
        del _lost_suspects[suspect]


def retry_failed_builds():
    for key in r.scan_iter("sid:jobs:*"):
        job = r.hgetall(key)
        if not (is_builder(job) and job.get("status") in BUILD_FAILED_STATES):
            continue
        if not job.get("build_attempt"):
            continue  # legacy record: audit history, never retried
        job_id = job.get("id") or key.rsplit(":", 1)[-1]
        attempt = _int(job.get("build_attempt"), 1)
        reason = failure_reason(job)[:3500]

        if attempt >= build_limit(job):
            r.hset(key, mapping={
                "status": "needs_human",
                "needs_human_kind": "build",
                "failed_status": job["status"],
                "needs_human_reason": f"build failed after {attempt} attempt(s): {reason}",
                "updated_at": now(),
            })
            print(f"[{ORCHESTRATOR_ID}] build job={job_id} needs_human", flush=True)
            continue

        next_attempt = attempt + 1
        if not r.hsetnx(key, f"retry_{next_attempt}_dispatched_at", now()):
            continue
        base_prompt = job.get("base_prompt") or job.get("prompt", "")
        prompt = (
            f"{base_prompt}\n\n"
            f"Attempt {attempt} of this job failed and was discarded:\n{reason}\n"
            "Start again from current main. Avoid what caused that failure and "
            "keep the change as small as the task allows."
        )
        release_stale_integration_lock(job_id)
        r.hdel(key, *BUILD_DERIVED_FIELDS)
        r.hset(key, mapping={
            "status": "queued",
            "build_attempt": str(next_attempt),
            "base_prompt": base_prompt,
            "prompt": prompt,
            "retry_reason": reason,
            "repair_attempts": "0",
            "updated_at": now(),
        })
        dispatch_job_once({"id": job_id}, {
            "id": job_id,
            "goal_id": job.get("goal_id", ""),
            "prompt": prompt,
            "provider": job.get("provider", DEFAULT_PROVIDER),
            "model": job.get("model", DEFAULT_MODEL),
            "role": "builder",
            "priority": _int(job.get("priority")),
            "created_at": job.get("created_at", now()),
        })
        print(f"[{ORCHESTRATOR_ID}] retry job={job_id} attempt={next_attempt}", flush=True)


def recover_stalled_reviews(now_ts=None):
    """Re-integrate and re-review a candidate whose review cannot finish:
    the reviewer failed, or the worker running its integration, review
    dispatch or repair died. Same operation as job-review.py reintegrate."""
    work = live_work()
    if work is None:
        return
    busy = work[0] | work[1]
    now_ts = time.time() if now_ts is None else now_ts
    seen = set()
    for key in r.scan_iter("sid:jobs:*"):
        builder = r.hgetall(key)
        if not is_builder(builder) or builder.get("status") != "awaiting_review":
            continue
        if builder.get("review_status") == "complete":
            continue  # resting: repair loop or human approval
        builder_id = builder.get("id") or key.rsplit(":", 1)[-1]
        related = {
            builder_id, builder.get("review_job_id"), builder.get("repair_job_id"),
            builder.get("last_repair_job_id"), builder.get("last_integrate_job_id"),
        } - {None, ""}
        if related & busy:
            continue
        if not confirmed_lost(f"stall:{builder_id}", seen, now_ts):
            continue
        del _lost_suspects[f"stall:{builder_id}"]
        reason = builder.get("review_error") or (
            f"review did not complete (review_status={builder.get('review_status') or 'none'}, "
            f"integration_status={builder.get('integration_status') or 'none'}; nothing in flight)"
        )
        recoveries = _int(builder.get("review_recoveries"))
        if recoveries >= MAX_REVIEW_RECOVERIES:
            r.hset(key, mapping={
                "status": "needs_human",
                "needs_human_kind": "review",
                "needs_human_reason": f"review could not complete after {recoveries} recovery attempt(s): {reason}",
                "updated_at": now(),
            })
            print(f"[{ORCHESTRATOR_ID}] review builder={builder_id} needs_human", flush=True)
            continue
        if not r.hsetnx(key, f"review_recovery_{recoveries + 1}_at", now()):
            continue
        release_stale_integration_lock(builder_id)
        integrate_id = uuid.uuid4().hex[:8]
        created = now()
        r.hdel(key, *DERIVED_REVIEW_FIELDS)
        r.hset(key, mapping={
            "last_integrate_job_id": integrate_id,
            "review_recoveries": str(recoveries + 1),
            "review_recovery_reason": reason[-2000:],
            "updated_at": created,
        })
        r.hset(f"sid:jobs:{integrate_id}", mapping={
            "id": integrate_id, "status": "queued", "role": "integrate",
            "target_builder_id": builder_id, "goal_id": builder.get("goal_id", ""),
            "created_at": created, "updated_at": created,
        })
        r.rpush(JOB_QUEUE, json.dumps({
            "id": integrate_id, "role": "integrate",
            "target_builder_id": builder_id, "created_at": created,
        }))
        print(f"[{ORCHESTRATOR_ID}] review recovery builder={builder_id} "
              f"integrate={integrate_id} ({recoveries + 1}/{MAX_REVIEW_RECOVERIES})", flush=True)
    for suspect in [s for s in _lost_suspects if s.startswith("stall:") and s not in seen]:
        del _lost_suspects[suspect]


def update_goals():
    for key in r.scan_iter("sid:goals:*"):
        # Planning reservations share the sid:goals:* namespace but are
        # string keys, not goal hashes. Never issue hash commands against
        # those transient lock keys.
        if key.endswith(":planning"):
            continue

        goal = r.hgetall(key)

        # Failed goals are also revisited because a child may later recover
        # through repair, reintegration, review, or human approval.
        if goal.get("status") not in {"running", "planning", "failed"}:
            continue

        try:
            job_ids = json.loads(goal.get("jobs", "[]"))
        except json.JSONDecodeError:
            continue

        if not job_ids:
            continue

        jobs = [r.hgetall(f"sid:jobs:{job_id}") for job_id in job_ids]
        states = [job.get("status") for job in jobs]

        if any(is_terminal_failure(job) for job in jobs):
            # Preserve historical failed-goal metadata while its children
            # remain terminally failed. If a child later recovers, this
            # branch stops matching and normal reconciliation can proceed.
            if goal.get("status") == "failed":
                continue
            r.hset(
                key,
                mapping={
                    "status": "failed",
                    "error": "child job reached a terminal failure state",
                    "updated_at": now(),
                },
            )
            continue

        if all(
            state in {"merged", "completed_no_changes"}
            for state in states
        ):
            # Preserve historical completed-goal metadata while its children
            # remain complete. A legacy error string is not a material state
            # change and must not churn updated_at every reconciliation loop.
            if goal.get("status") == "completed":
                continue
            r.hset(
                key,
                mapping={
                    "status": "completed",
                    "error": "",
                    "updated_at": now(),
                },
            )


def active_goal_id():
    ids = active_goal_ids()
    return ids[0] if ids else ""


def active_goal_ids():
    return [
        key.rsplit(":", 1)[-1]
        for key in sorted(r.scan_iter("sid:goals:*"))
        if ":planning" not in key
        and r.hget(key, "status") in {"planning", "running"}
    ]


def finish_goal_future(future):
    try:
        future.result()
    except Exception as exc:
        print(f"[{ORCHESTRATOR_ID}] goal failed: {exc}", flush=True)


def process_goal_safe(raw):
    try:
        process_goal(raw)
    except Exception as exc:
        try:
            goal = json.loads(raw)
            goal_id = goal.get("id", "unknown")
            r.hset(
                f"sid:goals:{goal_id}",
                mapping={
                    "status": "planning_failed",
                    "error": str(exc),
                    "updated_at": now(),
                },
            )
            r.delete(f"sid:goals:{goal_id}:planning")
        except Exception:
            pass
        raise


def submit_queued_goals(executor, futures):
    while len(futures) < MAX_CONCURRENT_GOALS:
        raw = r.lpop(GOAL_QUEUE)
        if not raw:
            break
        futures.add(executor.submit(process_goal_safe, raw))


def main():
    print(
        f"[{ORCHESTRATOR_ID}] started model={DEFAULT_MODEL}",
        flush=True,
    )

    futures = set()
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_GOALS) as executor:
        while True:
            try:
                for future in list(futures):
                    if future.done():
                        futures.remove(future)
                        finish_goal_future(future)

                recover_lost_jobs()
                retry_failed_builds()
                recover_stalled_reviews()
                queue_repairs()
                release_dependencies()
                update_goals()
                submit_queued_goals(executor, futures)

                ids = active_goal_ids()
                heartbeat("active", ids[0] if ids else "", ids) if ids else heartbeat()
                time.sleep(0.25 if futures else 1)
            except Exception as exc:
                print(f"[{ORCHESTRATOR_ID}] loop error: {exc}", flush=True)
                time.sleep(5)


if __name__ == "__main__":
    main()
