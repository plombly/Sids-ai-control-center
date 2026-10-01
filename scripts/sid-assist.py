#!/usr/bin/env python3
"""One turn of the goal assistant (services/goal_assist.py).

Started by the apps service as unit sid-assist-<session>:
    sid-assist.py SESSION_ID
Reads the session, asks Claude (ASSIST_MODEL, Haiku by default) with
read-only tools inside the project's sandbox, and stores its questions or
brief back in the session. It never submits a goal or changes a file.
"""

import json
import os
import sys
import time
from pathlib import Path

import redis as redis_lib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.append(str(ROOT / "apps/api"))
import agent_cli  # noqa: E402
import goal_assist  # noqa: E402
import project_reference  # noqa: E402
import project_sandbox  # noqa: E402
import sid_projects  # noqa: E402
import sid_redis  # noqa: E402

MODEL = os.getenv("ASSIST_MODEL", "claude-haiku-4-5-20251001")
BUDGET_USD = float(os.getenv("ASSIST_BUDGET_USD", "0.30"))
TIMEOUT = int(os.getenv("ASSIST_TIMEOUT_SECONDS", "150"))
MAX_RUNNING = int(os.getenv("ASSIST_MAX_CONCURRENT", "2"))
LOG_ROOT = Path(os.getenv("ASSIST_LOG_ROOT", "/var/log/sid-ai/assist"))


def connect():
    return redis_lib.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                    password=sid_redis.password(), decode_responses=True)


def recent_goals(r, project_id, limit=6):
    goals = []
    for key in r.scan_iter("sid:goals:*"):
        if key.count(":") != 2 or r.type(key) != "hash":
            continue
        goal = r.hgetall(key)
        if (goal.get("project_id") or "sid") == project_id and goal.get("status") in ("completed", "running", "planning", "queued"):
            first = next((line.strip() for line in str(goal.get("goal") or goal.get("prompt") or "").splitlines() if line.strip()), "")
            goals.append((float(goal.get("created_at") or 0), first[:140]))
    return [text for _, text in sorted(goals, reverse=True)[:limit] if text]


def kind_of(r, project_id):
    try:
        found = json.loads(r.get(f"sid:project-type:{project_id}") or "{}")
    except ValueError:
        found = {}
    chosen = r.hget(f"sid:projects:{project_id}", "type")
    description = r.hget(f"sid:projects:{project_id}", "type_description")
    kind = {"type": chosen or found.get("type"), "stack": found.get("stack")}
    if description:
        kind["type"] = f"{kind['type'] or 'unknown'}; the operator describes it as: {description}"
    return kind


def group_note(r, project):
    """For a project group: the other members' read-only copies (Claude may
    read them) and a note naming them; a parent's brief says which member
    each part of the work belongs to."""
    agent_cli.EXTRA_DIRS = []
    head = sid_projects.load(r, project.parent) if project.parent else project
    members = sid_projects.group(r, head)
    if len(members) < 2:
        return ""
    copies = project_reference.prepare(project, members)
    agent_cli.EXTRA_DIRS = [str(path) for _, path in copies]
    note = project_reference.note(project, head.name, copies)
    if not project.parent and note:
        note += ("A goal given here can plan work in any member. In the brief, say which member "
                 "(by id) each part of the work belongs to.\n")
    return note


def ask(r, session, project, force_brief=False, runner=agent_cli.run_claude):
    try:
        note = group_note(r, project)
    except Exception as exc:  # the group is context, never a reason to fail
        print(f"[assist] group context: {exc}", flush=True)
        note = ""
    prompt = goal_assist.build_prompt(project.name, project.id, kind_of(r, project.id), session["turns"],
                                      recent_goals(r, project.id), force_brief=force_brief, group_note=note)
    log_path = LOG_ROOT / f"{session['id']}-{len(session['turns'])}{'-retry' if force_brief else ''}.json"
    return runner("assistant", prompt, project.repo, log_path, TIMEOUT, model=MODEL, budget_usd=BUDGET_USD,
                  tools=agent_cli.READ_ONLY_TOOLS, system_prompt=goal_assist.SYSTEM_PROMPT,
                  wrap=lambda argv: project_sandbox.command(argv, project, project.repo, kind="agent", writable=False))


def fail(r, session_id, message):
    goal_assist.save(r, session_id, status="failed", error=message[:500])
    return 1


def main(session_id, r=None, runner=agent_cli.run_claude, wait=time.sleep):
    r = r or connect()
    session = goal_assist.load(r, session_id)
    if not session or session.get("status") != "queued":
        return 0  # cancelled, or already handled
    session["id"] = session_id
    try:
        project = sid_projects.load(r, session.get("project_id"))
    except LookupError:
        return fail(r, session_id, "The project no longer exists.")
    if agent_cli.claude_cooling_down(r):
        return fail(r, session_id, "Claude is paused after hitting a usage limit. Try again later, or use Send as written.")
    holder = f"assist:{session_id}"
    deadline = time.time() + 90
    while not r.eval(agent_cli._ACQUIRE, 1, goal_assist.SLOTS_KEY, str(time.time()), str(time.time() + TIMEOUT * 2 + 30),
                     holder, str(MAX_RUNNING)):
        if time.time() > deadline:
            return fail(r, session_id, "The assistant is busy with other requests. Try again in a minute.")
        wait(3)
    goal_assist.save(r, session_id, status="thinking", error="")
    try:
        cost = float(session.get("cost_usd") or 0)
        for force_brief in (False, True):
            run = ask(r, session, project, force_brief=force_brief, runner=runner)
            if run.result:
                cost += float(run.result.get("total_cost_usd") or 0)
            if not run.ok:
                if run.unavailable:
                    agent_cli.start_cooldown(r, f"claude unavailable: {run.describe_error()}")
                    return fail(r, session_id, "Claude is not available right now (usage limit or login). Use Send as written.")
                goal_assist.save(r, session_id, cost_usd=cost)
                return fail(r, session_id, f"The assistant failed: {run.describe_error()[:300]}")
            try:
                kind, value = goal_assist.parse_reply(run.text)
            except ValueError as exc:
                if force_brief:
                    goal_assist.save(r, session_id, cost_usd=cost)
                    return fail(r, session_id, str(exc))
                continue
            if kind == "questions" and (goal_assist.asked_before(session["turns"]) or force_brief):
                continue  # one round of questions only: ask again for a brief
            turns = session["turns"] + [{"from": "assistant", kind: value}]
            # Only a still-queued session is updated (the operator may have started over).
            if r.hget(goal_assist.session_key(session_id), "status") != "thinking":
                return 0
            goal_assist.save(r, session_id, status=kind, turns=turns, cost_usd=cost, model=MODEL,
                             **({"questions": value} if kind == "questions" else {"brief": value, "questions": []}))
            return 0
        return fail(r, session_id, "The assistant kept asking questions instead of writing a brief.")
    finally:
        r.zrem(goal_assist.SLOTS_KEY, holder)


if __name__ == "__main__":
    if len(sys.argv) != 2 or not goal_assist.SESSION_ID.fullmatch(sys.argv[1]):
        print("usage: sid-assist.py SESSION_ID", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
