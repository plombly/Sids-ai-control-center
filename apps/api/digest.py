"""The weekly digest: what SID did across all projects in the last 7 days.

Pure text building, shared by the API (dashboard "Preview digest") and the
host's scripts/sid-digest.py (posts it to Discord / ntfy on the schedule
from the settings page).
"""

import datetime
import json

import usage_routes

WEEK = 7 * 86400


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _tokens(value):
    value = _number(value)
    return f"{value / 1e6:.1f}M" if value >= 1e6 else f"{value / 1e3:.0f}k" if value >= 1e3 else str(int(value))


def _ago(seconds, now):
    if not seconds:
        return "never"
    age = now - seconds
    return f"{age / 3600:.0f} h ago" if age < 172800 else f"{age / 86400:.0f} days ago"


def build(now, projects, goals, jobs, events, backup=None, restore=None, apps=None, dashboard=""):
    """projects: [(id, name)]; goals/jobs: {id: record}; events: {project: [json]};
    apps: {project: app-status}. Returns (title, text)."""
    since = now - WEEK
    apps = apps or {}
    names = dict(projects)
    start = datetime.datetime.fromtimestamp(since).strftime("%b %d")
    end = datetime.datetime.fromtimestamp(now).strftime("%b %d")
    usage = usage_routes.summarize(jobs, goals, since)
    claude = next((row for row in usage["by_provider"] if row["provider"] == "claude"), {})
    codex = next((row for row in usage["by_provider"] if row["provider"] == "codex"), {})

    def project_of(record):
        return record.get("project_id") or "sid"

    lines, totals = [], {"done": 0, "failed": 0, "merged": 0, "undone": 0, "waiting": 0, "stuck": 0}
    for project_id, name in projects:
        done = sum(1 for g in goals.values() if project_of(g) == project_id and g.get("status") == "completed"
                   and _number(g.get("updated_at")) >= since)
        failed = sum(1 for g in goals.values() if project_of(g) == project_id and g.get("status") in ("failed", "planning_failed")
                     and _number(g.get("updated_at")) >= since)
        builders = [j for j in jobs.values() if project_of(j) == project_id and j.get("role", "builder") == "builder"]
        merged = sum(1 for j in builders if j.get("status") == "merged" and _number(j.get("merged_at")) >= since)
        waiting = sum(1 for j in builders if j.get("status") == "awaiting_review" and j.get("review_verdict") == "pass")
        stuck = sum(1 for j in builders if j.get("status") == "needs_human")
        undone = 0
        for raw in events.get(project_id, []):
            try:
                entry = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if entry.get("kind") == "undo" and _number(entry.get("at")) >= since:
                undone += 1
        for key, value in (("done", done), ("failed", failed), ("merged", merged), ("undone", undone),
                           ("waiting", waiting), ("stuck", stuck)):
            totals[key] += value
        app = (apps.get(project_id) or {}).get("state")
        if not any((done, failed, merged, undone, waiting, stuck)) and app not in ("crashed", "setup_failed"):
            continue  # nothing to say about this project
        parts = []
        if done or failed:
            parts.append(f"{done} goal{'s' if done != 1 else ''} done" + (f", {failed} failed" if failed else ""))
        if merged:
            parts.append(f"{merged} merged")
        if undone:
            parts.append(f"{undone} undone")
        if waiting:
            parts.append(f"**{waiting} waiting for approval**")
        if stuck:
            parts.append(f"**{stuck} stuck**")
        if app == "running":
            parts.append("app running")
        elif app in ("crashed", "setup_failed"):
            parts.append("**app down**")
        lines.append(f"• **{name or project_id}**: " + " · ".join(parts))

    summary = (f"{totals['done']} goals finished" + (f", {totals['failed']} failed" if totals["failed"] else "")
               + f" · {totals['merged']} changes merged" + (f" · {totals['undone']} undone" if totals["undone"] else "")
               + f" · ${_number(claude.get('cost_usd')):.2f} Claude · {_tokens(codex.get('effective_tokens'))} Codex tokens")
    body = [summary, ""]
    body += lines or ["Quiet week: no project activity."]
    health = []
    if backup:
        health.append(f"last backup {'OK' if backup.get('ok') else 'FAILED'} ({backup.get('at', '?')})")
    if restore:
        health.append(f"restore check {'OK' if restore.get('ok') else 'FAILED'} {_ago(_number(restore.get('at')), now)}")
    if health:
        body += ["", "Backups: " + " · ".join(health)]
    if totals["waiting"] or totals["stuck"]:
        body += ["", f"Needs you now: {totals['waiting']} to approve, {totals['stuck']} stuck"]
    if dashboard:
        body += [dashboard]
    return f"SID weekly digest · {start} – {end}", "\n".join(body)
