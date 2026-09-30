"""Project sandbox: what a project's gate and agents can see and write."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sid_testing import ROOT, load_module

sys.path.insert(0, str(ROOT / "services"))
import project_sandbox  # noqa: E402
import sid_projects  # noqa: E402


def make_project(tmp_path, pid="shop"):
    root = tmp_path / "projects" / pid
    repo, worktrees = root / "repo", root / "worktrees"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    worktrees.mkdir(parents=True)
    wt = worktrees / "job-1"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt)], check=True)
    (root / "deploy_key").write_text("PRIVATE")
    project = sid_projects.Project({"id": pid, "root": str(root), "repo": str(repo),
                                    "worktrees": str(worktrees), "logs": str(root / "logs")})
    return project, wt


@pytest.fixture
def hidden(monkeypatch, tmp_path):
    secret_dir = tmp_path / "sid-secrets"
    secret_dir.mkdir()
    (secret_dir / "operator.env").write_text("TOKEN")
    state = tmp_path / "agent-state"
    state.mkdir()
    monkeypatch.setattr(project_sandbox, "HIDDEN", (str(secret_dir), str(tmp_path / "projects")))
    monkeypatch.setattr(project_sandbox, "AGENT_STATE", (str(state),))
    return secret_dir, state


def test_sid_itself_is_never_wrapped(tmp_path):
    sid = sid_projects.Project(sid_projects.sid_defaults())
    assert project_sandbox.command(["echo", "x"], sid, tmp_path, kind="gate") == ["echo", "x"]
    assert project_sandbox.command(["echo", "x"], None, tmp_path, kind="agent") == ["echo", "x"]


def test_gate_is_offline_and_agents_get_only_their_state(tmp_path, hidden):
    project, wt = make_project(tmp_path)
    _, state = hidden
    gate = project_sandbox.command(["true"], project, wt, kind="gate")
    agent = project_sandbox.command(["true"], project, wt, kind="agent")
    assert "--unshare-net" in gate and "--unshare-net" not in agent
    assert str(state) not in gate and ["--bind", str(state), str(state)] == agent[agent.index(str(state)) - 1:][:3]
    joined = " ".join(gate)
    assert f"--bind {wt} {wt}" in joined and f"--ro-bind {wt / '.git'} {wt / '.git'}" in joined
    assert joined.index(f"--bind {wt} {wt}") < joined.index(f"--ro-bind {wt / '.git'}")
    assert f"--ro-bind /dev/null {project.root / 'deploy_key'}" in joined
    assert "--unsetenv REDIS_URL" in joined and gate[-2:] == ["--", "true"]


def test_planner_gets_no_writable_directory(tmp_path, hidden):
    project, _ = make_project(tmp_path)
    argv = project_sandbox.command(["true"], project, project.repo, kind="agent", writable=False)
    assert "--bind" not in [a for a in argv if a == "--bind" and argv[argv.index(a) + 1] == str(project.repo)]
    assert f"--bind {project.repo} {project.repo}" not in " ".join(argv)


def _bwrap_works():
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-pid", "--cap-drop", "ALL", "true"],
                          capture_output=True).returncode == 0


@pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap cannot run here")
def test_real_sandbox_confines_a_gate(tmp_path, hidden):
    project, wt = make_project(tmp_path)
    secret_dir, _ = hidden
    script = "; ".join([
        "touch made-here && echo W_OK",
        "touch /tmp/t && echo TMP_OK",
        f"touch {project.repo}/evil 2>/dev/null || echo REPO_RO",
        f"echo x > {project.repo}/.git/hooks/post-commit 2>/dev/null || echo HOOKS_RO",
        "echo 'gitdir: /tmp' > .git 2>/dev/null || echo POINTER_RO",
        f"cat {secret_dir}/operator.env 2>/dev/null || echo SECRET_HIDDEN",
        f"cat {project.root}/deploy_key",
        "echo KEY_END",
    ])
    argv = project_sandbox.command(["/bin/sh", "-c", script], project, wt, kind="gate")
    out = subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout
    for marker in ("W_OK", "TMP_OK", "REPO_RO", "HOOKS_RO", "POINTER_RO", "SECRET_HIDDEN"):
        assert marker in out, out
    assert "PRIVATE" not in out and "TOKEN" not in out
    assert (wt / "made-here").exists() and not (project.repo / "evil").exists()
    assert (wt / ".git").read_text().startswith("gitdir: ")


# --- worker: host git refuses tampered worktrees ---------------------------------------

@pytest.fixture
def worker(tmp_path):
    module = load_module(ROOT / "services/worker/worker.py")
    project, wt = make_project(tmp_path)
    module.use_project(project)
    return module, project, wt


def test_worker_git_runs_in_an_intact_worktree(worker):
    module, _, wt = worker
    assert module.run_git("status", "--short", cwd=wt).returncode == 0


def test_worker_git_refuses_a_redirected_pointer(worker, tmp_path):
    module, _, wt = worker
    (wt / ".git").write_text(f"gitdir: {tmp_path}/fake\n")
    with pytest.raises(RuntimeError, match="tampered"):
        module.run_git("status", cwd=wt)


def test_worker_git_refuses_a_planted_git_directory(worker):
    module, _, wt = worker
    (wt / ".git").unlink()
    (wt / ".git").mkdir()
    with pytest.raises(RuntimeError, match="tampered"):
        module.run_git("status", cwd=wt / ".")


def test_worker_git_refuses_a_missing_pointer(worker):
    module, _, wt = worker
    (wt / ".git").unlink()
    with pytest.raises(RuntimeError, match="no .git pointer"):
        module.run_git("status", cwd=wt)


def test_project_gate_runs_inside_the_sandbox(worker, monkeypatch):
    module, project, wt = worker
    project.gate_command = "echo gate"
    seen = []
    real = module.project_sandbox.command
    monkeypatch.setattr(module.project_sandbox, "command",
                        lambda argv, *a, **k: seen.append((argv, k)) or real(argv, *a, **k))
    module.project_gate(wt)
    assert seen and seen[0][1]["kind"] == "gate"
