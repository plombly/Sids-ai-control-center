"""Project sandbox: what a project's gate and agents can see and write."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from laika_testing import ROOT, load_module

sys.path.insert(0, str(ROOT / "services"))
import project_sandbox  # noqa: E402
import laika_projects  # noqa: E402


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
    project = laika_projects.Project({"id": pid, "root": str(root), "repo": str(repo),
                                    "worktrees": str(worktrees), "logs": str(root / "logs")})
    return project, wt


@pytest.fixture
def hidden(monkeypatch, tmp_path):
    secret_dir = tmp_path / "laika-secrets"
    secret_dir.mkdir()
    (secret_dir / "operator.env").write_text("TOKEN")
    state = tmp_path / "agent-state"
    state.mkdir()
    monkeypatch.setattr(project_sandbox, "HIDDEN", (str(secret_dir), str(tmp_path / "projects")))
    monkeypatch.setattr(project_sandbox, "agent_state", lambda home=None: (str(state),))
    return secret_dir, state


def test_laika_itself_is_never_wrapped(tmp_path):
    laika = laika_projects.Project(laika_projects.builtin_defaults())
    assert project_sandbox.command(["echo", "x"], laika, tmp_path, kind="gate") == ["echo", "x"]
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


# --- setup step: dependencies with network, never committed -------------------------

def test_setup_has_network_and_a_package_cache_but_gates_do_not(tmp_path, hidden):
    project, wt = make_project(tmp_path)
    setup = project_sandbox.command(["true"], project, wt, kind="setup")
    gate = project_sandbox.command(["true"], project, wt, kind="gate")
    assert "--unshare-net" not in setup and "--unshare-net" in gate
    cache = str(project.root / "cache")
    assert ["--bind", cache, cache] == setup[setup.index(cache) - 1:][:3]
    assert ["--setenv", "HOME", cache] == setup[setup.index("HOME") - 1:][:3]


def test_setup_output_is_never_committed_and_runs_once(worker, monkeypatch):
    module, project, wt = worker
    (wt / "tracked.txt").write_text("original\n")
    module.run_git("add", "tracked.txt", cwd=wt)
    module.run_git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "t", cwd=wt)
    project.setup_command = ("mkdir -p node_modules/.bin && echo dep > node_modules/dep.js && "
                             "echo lock > package-lock.json && echo changed >> tracked.txt && echo SETUP-RAN")
    project.gate_command = "test -f node_modules/dep.js && echo GATE-SAW-DEPS"
    ok, output = module.project_gate(wt)
    assert ok, output
    assert "SETUP-RAN" in output and "GATE-SAW-DEPS" in output
    status = module.run_git("status", "--porcelain", cwd=wt).stdout
    assert status == "", status  # installed files ignored, tracked file restored
    assert (wt / "tracked.txt").read_text() == "original\n"
    ok, output = module.project_gate(wt)
    assert ok and "SETUP-RAN" not in output  # once per worktree


def test_failed_setup_fails_the_gate_without_running_tests(worker):
    module, project, wt = worker
    project.setup_command = "echo cannot-download; exit 7"
    project.gate_command = "echo TESTS-RAN"
    ok, output = module.project_gate(wt)
    assert not ok and "cannot-download" in output and "TESTS-RAN" not in output


def test_commands_are_detected_when_not_configured(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "node test.js"}}')
    assert laika_projects.detect_gate(tmp_path) == "npm test"
    assert laika_projects.detect_setup(tmp_path).startswith("npm install")
    (tmp_path / "package-lock.json").write_text("{}")
    (tmp_path / "requirements.txt").write_text("flask\n")
    setup = laika_projects.detect_setup(tmp_path)
    assert setup.startswith("npm ci") and ".venv/bin/pip install -q pytest -r requirements.txt" in setup
    assert laika_projects.detect_setup(tmp_path / "missing") == ""


def test_codex_runs_inside_the_project_sandbox_with_its_own_sandbox_off(tmp_path, hidden):
    project, wt = make_project(tmp_path)
    codex = ["codex", "exec", "--sandbox", "workspace-write", "--cd", str(wt), "-"]
    argv = project_sandbox.codex_command(codex, project, wt)
    assert argv[0] == project_sandbox.BWRAP and "--cap-drop" in argv
    assert argv[argv.index("--") + 1:] == ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--cd", str(wt), "-"]
    assert f"--bind {wt} {wt}" in " ".join(argv)
    reviewer = project_sandbox.codex_command(codex, project, wt, writable=False)
    assert f"--bind {wt} {wt}" not in " ".join(reviewer)
    laika = laika_projects.Project(laika_projects.builtin_defaults())
    assert project_sandbox.codex_command(codex, laika, wt) == codex  # LAIka keeps Codex's own sandbox


def test_worker_wraps_codex_for_projects(worker, monkeypatch):
    module, project, wt = worker
    seen = []

    class Stop(Exception):
        pass

    def fake_popen(command, **kwargs):
        seen.append(command)
        raise Stop
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    with pytest.raises(Stop):
        module.run_codex({"id": "j1", "prompt": "x", "role": "builder"}, wt, wt.parent / "j1.jsonl")
    assert seen[0][0] == project_sandbox.BWRAP and "--dangerously-bypass-approvals-and-sandbox" in seen[0]


def test_caches_and_installed_dependencies_are_never_committed(worker):
    module, project, wt = worker
    module.ensure_standard_excludes(wt)
    module.ensure_standard_excludes(wt)  # idempotent
    for path in ("__pycache__/a.cpython-314.pyc", "tests/__pycache__/t.pyc", ".pytest_cache/v", "node_modules/x/i.js", ".venv/bin/python", "kept.txt"):
        target = wt / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    status = module.run_git("status", "--porcelain", "--untracked-files=all", cwd=wt).stdout
    assert status.strip() == "?? kept.txt", status
    exclude = (project.repo / ".git" / "info" / "exclude").read_text()
    assert exclude.count("__pycache__/") == 1


def test_agents_and_gates_write_no_python_caches(tmp_path, hidden):
    project, wt = make_project(tmp_path)
    for kind in ("agent", "gate"):
        argv = " ".join(project_sandbox.command(["true"], project, wt, kind=kind))
        assert "--setenv PYTHONDONTWRITEBYTECODE 1" in argv


def test_agent_state_follows_the_running_users_home(monkeypatch):
    monkeypatch.setenv("HOME", "/var/lib/laika/home")
    assert project_sandbox.agent_state() == ("/var/lib/laika/home/.codex", "/var/lib/laika/home/.claude",
                                             "/var/lib/laika/home/.claude.json")
    assert "/var/lib/laika/home" in project_sandbox.HIDDEN and "/var/lib/laika/db" in project_sandbox.HIDDEN


def test_secrets_are_removed_from_the_environment(tmp_path, hidden):
    project, wt = make_project(tmp_path)
    for kind in ("gate", "setup", "app", "agent"):
        joined = " ".join(project_sandbox.command(["true"], project, wt, kind=kind))
        for name in ("REDIS_PASSWORD", "REDIS_URL", "LAIKA_OPERATOR_TOKEN"):
            assert f"--unsetenv {name}" in joined, (kind, name)
        assert ("--unsetenv ANTHROPIC_API_KEY" in joined) == (kind != "agent"), kind
        assert ("--unsetenv OPENAI_API_KEY" in joined) == (kind != "agent"), kind
