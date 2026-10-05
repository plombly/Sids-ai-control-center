"""Monthly spending and its limits (standard library only: shared by the API
and the host's notifier).

What counts: every AI run LAIka records a cost for this calendar month (UTC:
the API container and the host can have different time zones, and both must
agree on when a month starts): jobs (builds, reviews, repairs), planning, and goal-box assistant
turns (questionnaires and conversations; their sessions expire after a day,
so their cost is also added to a monthly ledger when it is recorded:
laika:spend:<YYYY-MM>:project:<id> / :person:<name>). Claude reports what a
run would cost on the API even on a subscription; Codex on a ChatGPT plan
often reports nothing, which counts as $0.

Limits: a project's registry fields budget_usd / budget_mode and a person's
(laika:users:<name>) the same; 0 or empty = no limit. Mode "block" (the
default) refuses new goals and assistant turns once the month's spending
reached the limit; "warn" only notifies. Work already running always
finishes.
"""

import datetime
import time

WARN_AT = 0.8
MODES = ("block", "warn")
POLICIES = ("approvers", "admins", "off")


def policy(redis):
    """The SPENDING_LIMITS setting: approvers | admins | off."""
    try:
        import settings_schema
        value = settings_schema.values(redis).get("SPENDING_LIMITS", "approvers")
    except Exception:  # an unreadable setting must not switch limits off
        value = "approvers"
    return value if value in POLICIES else "approvers"


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


def _utc(now):
    return datetime.datetime.fromtimestamp(now if now is not None else time.time(), datetime.timezone.utc)


def month(now=None):
    return _utc(now).strftime("%Y-%m")


def month_start(now=None):
    moment = _utc(now)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def ledger_key(scope, name, now=None):
    return f"laika:spend:{month(now)}:{scope}:{name}"


def record_assistant(redis, project_id, person, cost, now=None):
    """Add an assistant turn's cost to this month's ledger."""
    if cost <= 0:
        return
    for scope, name in (("project", project_id), ("person", (person or "").lower())):
        if name:
            redis.incrbyfloat(ledger_key(scope, name, now), round(cost, 6))
            redis.expire(ledger_key(scope, name, now), 40 * 86400)


def totals(jobs, goals, since):
    """({project: $}, {person: $}) spent since `since` (jobs and planning)."""
    by_project, by_person = {}, {}

    def builder_of(job):
        for field in ("builder_job_id", "target_builder_id"):
            builder = jobs.get(job.get(field) or "")
            if builder:
                return builder
        return job

    def add(table, key, cost):
        if key and cost:
            table[key] = table.get(key, 0.0) + cost

    for job in jobs.values():
        if _number(job.get("created_at") or job.get("updated_at")) < since:
            continue
        cost = _number(job.get("cost_usd"))
        owner = builder_of(job)
        add(by_project, job.get("project_id") or owner.get("project_id") or "laika", cost)
        goal = goals.get(owner.get("goal_id") or "") or {}
        add(by_person, (goal.get("submitted_by") or "").lower(), cost)
    for goal in goals.values():
        if _number(goal.get("created_at") or goal.get("updated_at")) < since:
            continue
        cost = _number(goal.get("planner_cost_usd"))
        add(by_project, goal.get("project_id") or "laika", cost)
        add(by_person, (goal.get("submitted_by") or "").lower(), cost)
    return by_project, by_person


def limit_of(record):
    budget = _number(record.get("budget_usd"))
    mode = record.get("budget_mode") if record.get("budget_mode") in MODES else "block"
    return budget, mode


def state(spent, budget, mode="block"):
    """What the dashboard and the notifier show."""
    if budget <= 0:
        return {"spent": round(spent, 2), "budget": 0, "mode": mode, "percent": 0, "state": "none"}
    share = spent / budget
    return {"spent": round(spent, 2), "budget": round(budget, 2), "mode": mode, "percent": round(share * 100),
            "state": "over" if share >= 1 else "warn" if share >= WARN_AT else "ok"}


def ledger(redis, scope, name, now=None):
    return _number(redis.get(ledger_key(scope, name, now)) if name else 0)


def project_state(redis, project_id, record, by_project, now=None, rule=None):
    budget, mode = limit_of(record)
    rule = rule or policy(redis)
    current = state(by_project.get(project_id, 0.0) + ledger(redis, "project", project_id, now),
                    0 if rule == "off" else budget, mode)
    return {**current, "policy": rule}


def person_state(redis, name, record, by_person, now=None, rule=None):
    budget, mode = limit_of(record)
    rule = rule or policy(redis)
    current = state(by_person.get(name, 0.0) + ledger(redis, "person", name, now), 0 if rule == "off" else budget, mode)
    return {**current, "policy": rule}


def blocked(project_state_, person_state_):
    """The refusal message, or "" when new work may start."""
    for which, current, raiser in (("This project's", project_state_, "someone who may approve this project"),
                                   ("Your", person_state_, "an administrator")):
        if current and current["state"] == "over" and current["mode"] == "block":
            return (f"{which} monthly spending limit is used up (${current['spent']:.2f} of ${current['budget']:.2f} "
                    f"this month). New goals wait until next month or until {raiser} raises the limit.")
    return ""
