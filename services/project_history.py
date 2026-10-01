"""A project's recent history on main, grouped the way you think about it.

Commits that came from one SID job (its builder, repairs and integration,
merged together) form one change, titled with the job's title; anything
else (uploads and file edits from the dashboard, undo commits, hand-made
commits) is a change of its own. The apps service publishes this per
project as JSON in sid:history:<id> whenever main moves (the API container
has no git); the dashboard shows it and offers "Undo" for project changes.
"""

import json
import subprocess

FIELD = "\x1f"


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "git failed")
    return result.stdout


def head(repo, branch="main"):
    return _git(repo, "rev-parse", f"refs/heads/{branch}").strip()


def build(repo, jobs, branch="main", limit=40):
    """[change], newest first. jobs: merged job records of this project."""
    raw = _git(repo, "log", f"-n{limit}", f"--format=%H{FIELD}%an{FIELD}%at{FIELD}%s", f"refs/heads/{branch}")
    commits = []
    for line in raw.splitlines():
        parts = line.split(FIELD)
        if len(parts) == 4:
            sha, author, when, subject = parts
            commits.append({"sha": sha, "author": author, "time": int(when or 0), "subject": subject})
    owner = {}
    for job in jobs:
        base, merged = job.get("integration_base_commit"), job.get("merge_commit")
        if not (base and merged):
            continue
        try:
            shas = _git(repo, "rev-list", "--max-count=200", f"{base}..{merged}").split()
        except RuntimeError:
            continue  # history rewritten or commits gone: show them as plain commits
        for sha in shas:
            owner.setdefault(sha, job)
    changes = []
    for commit in commits:
        job = owner.get(commit["sha"])
        last = changes[-1] if changes else None
        if job and last and last.get("job_id") == job.get("id"):
            last["commits"].append(commit)
            continue
        if job:
            changes.append({"kind": "job", "job_id": job.get("id"), "title": job.get("title") or commit["subject"],
                            "time": commit["time"], "author": "SID", "head": commit["sha"],
                            "base": job.get("integration_base_commit"), "commits": [commit]})
        else:
            changes.append({"kind": "commit", "sha": commit["sha"], "title": commit["subject"], "time": commit["time"],
                            "author": commit["author"], "commits": [commit]})
    for change in changes:
        # Complete only if every commit of the job is in view (older ones may be cut off).
        if change["kind"] == "job":
            change["complete"] = change["commits"][-1]["sha"] != commits[-1]["sha"] or len(commits) < limit
    return changes


def publish(redis, project, load_jobs, branch="main"):
    """Store the history when main moved; returns True if it was rebuilt.
    load_jobs() -> merged job records of this project (only called then)."""
    key = f"sid:history:{project.id}"
    current = head(project.repo, branch)
    try:
        stored = json.loads(redis.get(key) or "{}")
    except ValueError:
        stored = {}
    if stored.get("head") == current:
        return False
    changes = build(project.repo, load_jobs(), branch)
    redis.set(key, json.dumps({"head": current, "changes": changes}))
    return True
