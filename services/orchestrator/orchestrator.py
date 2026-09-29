#!/usr/bin/env python3

import json
import os
import subprocess
import time
import uuid
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

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def now():
    return str(time.time())


def heartbeat(status="idle", goal_id=""):
    r.hset(
        f"sid:orchestrators:{ORCHESTRATOR_ID}",
        mapping={
            "id": ORCHESTRATOR_ID,
            "status": status,
            "goal_id": goal_id,
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
        "created_at": now(),
        "updated_at": now(),
    }

    r.hset(f"sid:jobs:{job_id}", mapping=record)

    if not dependencies:
        r.rpush(JOB_QUEUE, json.dumps({
            "id": job_id,
            "goal_id": goal_id,
            "prompt": planned_job["task"],
            "provider": DEFAULT_PROVIDER,
            "model": DEFAULT_MODEL,
            "role": "builder",
            "priority": 0,
            "created_at": record["created_at"],
        }))

    return job_id



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
            r.rpush(
                JOB_QUEUE,
                json.dumps({
                    "id": job_id,
                    "goal_id": goal_id,
                    "prompt": scoped_builder_prompt(item),
                    "provider": DEFAULT_PROVIDER,
                    "model": DEFAULT_MODEL,
                    "role": "builder",
                    "priority": 0,
                    "created_at": record["created_at"],
                }),
            )

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

        if attempts >= MAX_REPAIR_ATTEMPTS:
            if builder.get("repair_status") != "exhausted":
                r.hset(
                    key,
                    mapping={
                        "status": "repair_exhausted",
                        "repair_status": "exhausted",
                        "updated_at": now(),
                    },
                )
            continue

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
{next_attempt} of {MAX_REPAIR_ATTEMPTS}

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
                f"attempt={next_attempt}/{MAX_REPAIR_ATTEMPTS}",
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

        dep_states = [
            r.hget(f"sid:jobs:{dep}", "status")
            for dep in deps
        ]

        failed_states = {
            "failed",
            "test_failed",
            "integration_failed",
            "rejected",
            "repair_exhausted",
        }

        if any(state in failed_states for state in dep_states):
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

        # Atomic state claim prevents duplicate release.
        if r.hsetnx(key, "released_at", now()):
            r.hset(
                key,
                mapping={
                    "status": "queued",
                    "updated_at": now(),
                },
            )
            r.rpush(JOB_QUEUE, json.dumps(payload))

            print(
                f"[{ORCHESTRATOR_ID}] released job={job_id}",
                flush=True,
            )


def update_goals():
    for key in r.scan_iter("sid:goals:*"):
        goal = r.hgetall(key)

        if goal.get("status") not in {"running", "planning"}:
            continue

        try:
            job_ids = json.loads(goal.get("jobs", "[]"))
        except json.JSONDecodeError:
            continue

        if not job_ids:
            continue

        states = [
            r.hget(f"sid:jobs:{job_id}", "status")
            for job_id in job_ids
        ]

        if any(state in {"failed", "test_failed", "integration_failed", "rejected", "repair_exhausted", "blocked_failed_dependency"} for state in states):
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
            r.hset(
                key,
                mapping={
                    "status": "completed",
                    "updated_at": now(),
                },
            )


def main():
    print(
        f"[{ORCHESTRATOR_ID}] started model={DEFAULT_MODEL}",
        flush=True,
    )

    while True:
        try:
            heartbeat()
            queue_repairs()
            release_dependencies()
            update_goals()

            item = r.blpop(GOAL_QUEUE, timeout=5)

            if item:
                _, raw = item

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
                    except Exception:
                        pass

                    print(
                        f"[{ORCHESTRATOR_ID}] goal failed: {exc}",
                        flush=True,
                    )

        except Exception as exc:
            print(
                f"[{ORCHESTRATOR_ID}] loop error: {exc}",
                flush=True,
            )
            time.sleep(5)


if __name__ == "__main__":
    main()
