"""Usage and cost: tokens and dollars per project, day, provider and role.

Sums what jobs already record (effective/input/output/cached tokens,
Claude's cost_usd, run time) plus the planner's cost on each goal. Reviews,
repairs and integrations count toward their builder's project. Claude cost
is what the CLI reports against the claude.ai plan; Codex reports tokens
only (it runs on the ChatGPT plan), so its cost shows as tokens.
"""

import datetime
import time
from collections import defaultdict

from fastapi import APIRouter, Query

router = APIRouter()

TOKEN_FIELDS = ("effective_tokens", "input_tokens", "output_tokens", "cached_input_tokens")


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


def _day(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).strftime("%Y-%m-%d")


def summarize(jobs, goals, since, only_project=None):
    """jobs/goals: {id: record}. Returns totals, by_project, by_day, by_provider, by_role."""
    def project_of(job):
        if job.get("project_id"):
            return job["project_id"]
        for field in ("builder_job_id", "target_builder_id"):
            builder = jobs.get(job.get(field) or "")
            if builder and builder.get("project_id"):
                return builder["project_id"]
        return "laika"

    def bucket():
        return {"jobs": 0, "cost_usd": 0.0, "seconds": 0.0, **{field: 0 for field in TOKEN_FIELDS}}

    groups = {"total": bucket(), "by_project": defaultdict(bucket), "by_day": defaultdict(bucket),
              "by_provider": defaultdict(bucket), "by_role": defaultdict(bucket)}

    def add(when, project, provider, role, tokens, cost, seconds, count=1):
        keys = (("by_project", project), ("by_day", _day(when)), ("by_provider", provider or "unknown"),
                ("by_role", role or "builder"))
        for target in [groups["total"], *(groups[name][key] for name, key in keys)]:
            target["jobs"] += count
            target["cost_usd"] += cost
            target["seconds"] += seconds
            for field in TOKEN_FIELDS:
                target[field] += int(tokens.get(field, 0))

    for job in jobs.values():
        when = _number(job.get("created_at") or job.get("updated_at"))
        if when < since:
            continue
        if only_project and project_of(job) != only_project:
            continue
        tokens = {field: _number(job.get(field)) for field in TOKEN_FIELDS}
        add(when, project_of(job), job.get("provider"), job.get("role"), tokens,
            _number(job.get("cost_usd")), _number(job.get("duration_seconds")))
    for goal in goals.values():
        when = _number(goal.get("created_at") or goal.get("updated_at"))
        cost = _number(goal.get("planner_cost_usd"))
        if when < since or not goal.get("planner_provider"):
            continue
        if only_project and (goal.get("project_id") or "laika") != only_project:
            continue
        add(when, goal.get("project_id") or "laika", goal.get("planner_provider"), "planner", {}, cost, 0)

    def rows(table, key_name):
        out = [{key_name: key, **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in value.items()}}
               for key, value in table.items()]
        return sorted(out, key=lambda row: row[key_name])

    total = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in groups["total"].items()}
    return {"since": since, "total": total, "by_project": rows(groups["by_project"], "project"),
            "by_day": rows(groups["by_day"], "day"), "by_provider": rows(groups["by_provider"], "provider"),
            "by_role": rows(groups["by_role"], "role")}


@router.get("/api/usage")
def usage(days: int = Query(default=30, ge=1, le=365), project: str = Query(default="", max_length=40)):
    import main
    jobs = {key.split(":", 2)[2]: data for key, data in main._hashes("laika:jobs:*")}
    goals = {key.split(":", 2)[2]: data for key, data in main._hashes("laika:goals:*")}
    result = summarize(jobs, goals, time.time() - days * 86400, only_project=project or None)
    result["days"] = days
    result["project"] = project or None
    return result
