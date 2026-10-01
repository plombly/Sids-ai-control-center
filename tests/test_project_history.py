"""services/project_history.py: main's history grouped by job."""

import json
import subprocess
import sys

from sid_testing import ROOT, MemoryRedis

sys.path.insert(0, str(ROOT / "services"))
import project_history  # noqa: E402
import sid_projects  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, name, message):
    (repo / name).write_text(message)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def test_commits_of_one_job_form_one_change(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    base = commit(repo, "a", "initial")
    commit(repo, "b", "builder work")
    merged = commit(repo, "c", "repair work")
    commit(repo, "d", "Upload d from the dashboard")
    jobs = [{"id": "j1", "title": "Add b and c", "integration_base_commit": base, "merge_commit": merged}]
    changes = project_history.build(repo, jobs)
    assert [c["kind"] for c in changes] == ["commit", "job", "commit"]
    assert changes[0]["title"] == "Upload d from the dashboard"
    assert changes[2]["root"] is True and changes[0]["root"] is False
    assert changes[1]["title"] == "Add b and c" and len(changes[1]["commits"]) == 2 and changes[1]["base"] == base
    r = MemoryRedis()
    project = sid_projects.Project({"id": "shop", "repo": str(repo), "worktrees": str(tmp_path), "logs": str(tmp_path)})
    calls = []
    assert project_history.publish(r, project, lambda: calls.append(1) or jobs) is True
    assert project_history.publish(r, project, lambda: calls.append(1) or jobs) is False  # main did not move
    assert len(calls) == 1 and json.loads(r.values["sid:history:shop"])["changes"][1]["job_id"] == "j1"
