"""The weekly digest: what LAIka did across all projects in the last 7 days.

Pure text building, shared by the API (dashboard "Preview digest") and the
host's scripts/laika-digest.py (posts it to Discord / ntfy on the schedule
from the settings page).
"""

import datetime
import json

import usage_core

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


def build(now, projects, goals, jobs, events, backup=None, restore=None, apps=None, dashboard="", parents=None):
    """projects: [(id, name)]; goals/jobs: {id: record}; events: {project: [json]};
    apps: {project: app-status}; parents: {child: parent} (project groups:
    a parent's line sums its group, its children follow indented).
    Returns (title, text)."""
    parents = {child: parent for child, parent in (parents or {}).items() if parent}
    since = now - WEEK
    apps = apps or {}
    names = dict(projects)
    start = datetime.datetime.fromtimestamp(since).strftime("%b %d")
    end = datetime.datetime.fromtimestamp(now).strftime("%b %d")
    usage = usage_core.summarize(jobs, goals, since)
    claude = next((row for row in usage["by_provider"] if row["provider"] == "claude"), {})
    codex = next((row for row in usage["by_provider"] if row["provider"] == "codex"), {})

    def project_of(record):
        return record.get("project_id") or "laika"

    totals = {"done": 0, "failed": 0, "merged": 0, "undone": 0, "waiting": 0, "stuck": 0}
    found = {}  # project -> (counts, parts) when there is something to say
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
        counts = {"done": done, "failed": failed, "merged": merged, "waiting": waiting, "stuck": stuck}
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
        found[project_id] = (counts, parts)

    lines = []
    known = {project_id for project_id, _ in projects}
    for project_id, name in projects:
        if parents.get(project_id) in known:
            continue  # listed under its parent
        children = [(child, child_name) for child, child_name in projects
                    if parents.get(child) == project_id and child in found]
        if not children:
            if project_id in found:
                lines.append(f"• **{name or project_id}**: " + " · ".join(found[project_id][1]))
            continue
        members = [project_id] + [child for child, _ in children]
        summed = {key: sum(found[m][0][key] for m in members if m in found) for key in ("done", "merged", "waiting", "stuck")}
        group_parts = [f"{summed['done']} goal{'s' if summed['done'] != 1 else ''} done", f"{summed['merged']} merged"]
        if summed["waiting"]:
            group_parts.append(f"**{summed['waiting']} waiting for approval**")
        if summed["stuck"]:
            group_parts.append(f"**{summed['stuck']} stuck**")
        lines.append(f"• **{name or project_id}** (group of {len(members)}): " + " · ".join(group_parts))
        for member, member_name in [(project_id, name)] + children:
            if member in found:
                lines.append(f"    ◦ {member_name or member}: " + " · ".join(found[member][1]))

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
    return f"LAIka weekly digest · {start} – {end}", "\n".join(body)
