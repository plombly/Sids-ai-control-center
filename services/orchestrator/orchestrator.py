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
PLAN_TIMEOUT = int(os.getenv("PLAN_TIMEOUT", "900"))

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


def planner_prompt(goal):
    return f"""
You are the planning agent for SID's AI Command Center.

Repository:
{REPO_ROOT}

High-level goal:
{goal}

Inspect the repository before planning.

Break the goal into a SMALL set of implementation jobs that can be executed
by independent coding agents.

Rules:
- Do not modify files.
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
      "depends_on": []
    }}
  ]
}}
""".strip()


def run_planner(goal):
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
        input=planner_prompt(goal),
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


def validate_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")

    jobs = plan.get("jobs")

    if not isinstance(jobs, list) or not jobs:
        raise ValueError("plan contains no jobs")

    if len(jobs) > 10:
        raise ValueError("planner produced too many jobs")

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

    plan = run_planner(goal)
    jobs = validate_plan(plan)

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
            "prompt": item["task"],
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
            r.rpush(
                JOB_QUEUE,
                json.dumps({
                    "id": job_id,
                    "goal_id": goal_id,
                    "prompt": item["task"],
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
