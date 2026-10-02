"""A project's activity: one timeline of what happened.

Merges what Redis already records (goals started and finished, jobs merged,
rejected or stuck) with the short event log hosts write for everything
else (laika:events:<id>: dashboard file changes and uploads, undos, app
deploys and crashes, previews, restores; services/laika_projects.record_event).
"""

import json

from fastapi import APIRouter, HTTPException, Query

import project_routes as projects

router = APIRouter()

FINISHED = {"completed": ("goal_done", "Goal finished"), "failed": ("goal_failed", "Goal failed"),
            "planning_failed": ("goal_failed", "Goal could not be planned")}


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _line(value, limit=140):
    text = next((line.strip() for line in str(value or "").splitlines() if line.strip()), "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def timeline(project_id, goals, jobs, logged, limit=80):
    entries = []
    for goal_id, goal in goals.items():
        if (goal.get("project_id") or "laika") != project_id:
            continue
        title = _line(goal.get("summary") or goal.get("goal") or goal.get("prompt"))
        if goal.get("created_at"):
            entries.append({"at": _number(goal["created_at"]), "kind": "goal_started", "title": f"Goal started: {title}",
                            "ref": goal_id})
        if goal.get("status") in FINISHED:
            kind, label = FINISHED[goal["status"]]
            entries.append({"at": _number(goal.get("updated_at")), "kind": kind, "title": f"{label}: {title}", "ref": goal_id})
    for job_id, job in jobs.items():
        if (job.get("project_id") or "laika") != project_id or job.get("role", "builder") != "builder":
            continue
        title = _line(job.get("title") or job.get("prompt"))
        status = job.get("status")
        if status == "merged":
            entries.append({"at": _number(job.get("merged_at") or job.get("updated_at")), "kind": "merged",
                            "title": f"Approved and merged: {title}", "ref": job_id})
        elif status == "rejected":
            entries.append({"at": _number(job.get("updated_at")), "kind": "rejected", "title": f"Rejected: {title}", "ref": job_id})
        elif status == "needs_human":
            entries.append({"at": _number(job.get("updated_at")), "kind": "stuck", "title": f"Stuck: {title}", "ref": job_id})
    for raw in logged:
        try:
            entry = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict) and entry.get("kind"):
            entries.append({"at": _number(entry.get("at")), "kind": str(entry["kind"]), "title": str(entry.get("title", "")),
                            "detail": str(entry.get("detail", "")), "ref": str(entry.get("ref", ""))})
    entries.sort(key=lambda item: -item["at"])
    return entries[:limit]


def group_timeline(members, names, goals, jobs, logged, limit=80):
    """One timeline for a whole project group; every entry names its project."""
    entries = []
    for member in members:
        for entry in timeline(member, goals, jobs, logged.get(member, []), limit):
            entries.append({**entry, "project_id": member, "project_name": names.get(member, member)})
    entries.sort(key=lambda item: -item["at"])
    return entries[:limit]


@router.get("/api/projects/{project_id}/activity")
def activity(project_id: str, limit: int = Query(default=80, ge=1, le=500), group: bool = False):
    """A project's timeline; group=true: its whole group (parent and children)."""
    import main
    project_id = projects._id(project_id)
    if not projects._known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    goals = {key.split(":", 2)[2]: data for key, data in main._hashes("laika:goals:*")}
    jobs = {key.split(":", 2)[2]: data for key, data in main._hashes("laika:jobs:*")}

    def events_of(member):
        try:
            return main.redis.lrange(f"laika:events:{member}", 0, 499) or []
        except Exception:
            return []
    if group:
        parent_id, members = projects.group_of(project_id)
        members = [m for m in members if projects._known(m)]
        names = {m: projects._text(projects._project_data(m).get("name"), m) for m in members}
        return {"project_id": project_id, "group": parent_id, "members": members,
                "events": group_timeline(members, names, goals, jobs, {m: events_of(m) for m in members}, limit)}
    return {"project_id": project_id, "events": timeline(project_id, goals, jobs, events_of(project_id), limit)}
