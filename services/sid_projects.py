"""Project resolution shared by the worker, orchestrator and job-review.py.

SID runs several fully separated projects. Each has its own git repository,
worktree directory, log directory, test gate, merge queue, approval lock and
main-head record; nothing is shared but the worker pool. Projects are
registered by scripts/sid-project.py as Redis hashes sid:projects:<id>.

The SID codebase itself is project "sid". Its paths come from the existing
environment variables and its Redis keys keep their original names, so
single-project behavior is unchanged when nothing else is registered.
"""

import os
import re
from pathlib import Path

SID_PROJECT = "sid"
PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
IMPORTANCE = ("high", "medium", "low")


class Project:
    """A resolved project: paths, gate, importance and its Redis key names."""

    def __init__(self, fields):
        self.id = fields["id"]
        self.name = fields.get("name") or self.id
        self.repo = Path(fields["repo"])
        # The project's own directory (holds repo, worktrees, logs, deploy key).
        self.root = Path(fields.get("root") or Path(fields["repo"]).parent)
        self.worktrees = Path(fields["worktrees"])
        self.logs = Path(fields["logs"])
        self.default_branch = fields.get("default_branch") or "main"
        self.gate_command = fields.get("gate_command") or ""
        importance = fields.get("importance") or "medium"
        self.importance = importance if importance in IMPORTANCE else "medium"
        self.status = fields.get("status") or "active"

    @property
    def is_sid(self):
        return self.id == SID_PROJECT

    # Redis keys: SID keeps its original names.
    @property
    def merge_queue_key(self):
        return "sid:merge-queue" if self.is_sid else f"sid:merge-queue:{self.id}"

    @property
    def approval_lock_key(self):
        # Repository-wide (per project), never per job: approval serializes
        # every advancement of that project's main.
        return "sid:approval-lock:main" if self.is_sid else f"sid:approval-lock:{self.id}"

    @property
    def main_head_key(self):
        return "sid:main-head" if self.is_sid else f"sid:main-head:{self.id}"

    def __repr__(self):
        return f"Project({self.id!r}, repo={str(self.repo)!r})"


def sid_defaults():
    return {
        "id": SID_PROJECT,
        "name": "SID AI Command Center",
        "repo": os.getenv("REPO_ROOT", "/opt/sids-ai-command-center"),
        "worktrees": os.getenv("WORKTREE_ROOT", "/opt/sid-worktrees"),
        "logs": os.getenv("LOG_ROOT", "/var/log/sid-ai/jobs"),
        "default_branch": "main",
        "gate_command": "",  # SID uses its own gate (scripts/integration-check.py)
        "importance": "medium",
    }


def load(redis_client, project_id):
    """The project for this id. Unknown, empty or unregistered "sid" resolve
    to SID's defaults (registry fields override them); any other unknown id
    raises, so work never runs in the wrong repository."""
    project_id = project_id or SID_PROJECT
    fields = {}
    try:
        fields = redis_client.hgetall(f"sid:projects:{project_id}") or {}
    except Exception:
        if project_id != SID_PROJECT:
            raise
    if project_id == SID_PROJECT:
        merged = sid_defaults()
        # Only descriptive fields may be overridden for SID; its paths and its
        # gate (scripts/integration-check.py) stay the configured ones so a
        # registry entry cannot move or weaken the control plane.
        for key in ("name", "importance", "status"):  # never paths or the gate
            if fields.get(key):
                merged[key] = fields[key]
        return Project(merged)
    if not fields:
        raise LookupError(f"unknown project: {project_id}")
    return Project(fields)


def job_project_id(redis_client, job):
    """A job's project: its own field, else its builder's (reviews, repairs
    and integrations act on a builder), else SID."""
    project_id = job.get("project_id")
    if project_id:
        return project_id
    for field in ("builder_job_id", "target_builder_id"):
        builder_id = job.get(field)
        if builder_id:
            try:
                value = redis_client.hget(f"sid:jobs:{builder_id}", "project_id")
            except Exception:
                value = None
            if value:
                return value
    job_id = job.get("id")
    if job_id:
        try:
            value = redis_client.hget(f"sid:jobs:{job_id}", "project_id")
        except Exception:
            value = None
        if value:
            return value
    return SID_PROJECT


def all_projects(redis_client):
    """Every active project, SID first."""
    ids = set()
    try:
        ids = set(redis_client.smembers("sid:projects") or [])
    except Exception:
        pass
    ids.discard(SID_PROJECT)
    projects = [load(redis_client, SID_PROJECT)]
    for project_id in sorted(ids):
        try:
            project = load(redis_client, project_id)
        except LookupError:
            continue
        if project.status == "active":
            projects.append(project)
    return projects
