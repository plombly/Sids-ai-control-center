"""The worker is the attacker: candidate_safety, safe_git, trusted gates and
guard_git_control, against real git repositories."""

import os
import subprocess

import pytest

from laika_testing import ROOT, load_module

safety = load_module(ROOT / "services/candidate_safety.py", "candidate_safety_test")
safe_git = load_module(ROOT / "services/safe_git.py", "safe_git_test")
projects = load_module(ROOT / "services/laika_projects.py", "laika_projects_gate_test")


def repo(tmp_path):
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    run = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=path,
                                    check=True, capture_output=True, text=True).stdout
    (path / "app.py").write_text("print(1)\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    return path, run


def check(path, run, base):
    head = run("rev-parse", "HEAD").strip()
    return safety.inspect(lambda *a: run(*a), base, head)


def test_plain_changes_pass_and_sensitive_files_are_named(tmp_path):
    path, run = repo(tmp_path)
    base = run("rev-parse", "HEAD").strip()
    (path / "app.py").write_text("print(2)\n")
    (path / "conftest.py").write_text("")
    os.makedirs(path / ".github" / "workflows")
    (path / ".github/workflows/ci.yml").write_text("on: push\n")
    os.symlink("app.py", path / "alias.py")                       # a link inside the repo is fine
    run("add", "-A")
    run("commit", "-qm", "change")
    found = check(path, run, base)
    assert found["refused"] == []
    assert found["sensitive"] == [".github/workflows/ci.yml", "conftest.py"]


@pytest.mark.parametrize("target,why", [
    ("/etc/laika/operator.env", "absolute path"),
    ("../../outside", "leaving the repository"),
    ("../../../x", "leaving the repository"),
])
def test_links_out_of_the_repository_are_refused(tmp_path, target, why):
    path, run = repo(tmp_path)
    base = run("rev-parse", "HEAD").strip()
    os.makedirs(path / "sub", exist_ok=True)
    os.symlink(target, path / "sub" / "link")
    run("add", "-A")
    run("commit", "-qm", "link")
    assert why in " ".join(check(path, run, base)["refused"])


def test_submodules_and_unrelated_history_are_refused(tmp_path):
    path, run = repo(tmp_path)
    base = run("rev-parse", "HEAD").strip()
    run("update-index", "--add", "--cacheinfo", "160000," + base + ",vendor/lib")
    run("commit", "-qm", "gitlink")
    assert "submodule" in " ".join(check(path, run, base)["refused"])
    run("checkout", "-q", "--orphan", "other")
    run("commit", "-qm", "unrelated", "--allow-empty")
    assert "does not build on" in check(path, run, base)["refused"][0]


def test_unsafe_path_names():
    assert safety.unsafe_path(".GIT/config") and safety.unsafe_path("a/../b") and safety.unsafe_path("-rf")
    assert safety.unsafe_path("a\nb") and not safety.unsafe_path("src/.gitignore") and not safety.unsafe_path("a/b.py")


def test_hardened_git_runs_no_hooks_and_drops_redirection(tmp_path):
    path, run = repo(tmp_path)
    hook = path / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\ntouch ran-the-hook\n")
    hook.chmod(0o755)
    (path / "b.py").write_text("x\n")
    run("add", "-A")
    subprocess.run(safe_git.command("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"),
                   cwd=path, check=True, env=safe_git.env())
    assert not (path / "ran-the-hook").exists()
    env = safe_git.env({"GIT_DIR": "/elsewhere", "GIT_CONFIG_KEY_0": "x", "GIT_CONFIG_GLOBAL": "/etc/laika/gitconfig",
                        "PATH": "/bin"})
    assert env == {"GIT_CONFIG_GLOBAL": "/etc/laika/gitconfig", "PATH": "/bin"}


def test_gate_detection_reads_the_trusted_base_commit(tmp_path):
    path, run = repo(tmp_path)
    (path / "package.json").write_text('{"scripts": {"test": "node --test"}}')
    run("add", "-A")
    run("commit", "-qm", "tests")
    base = run("rev-parse", "HEAD").strip()
    (path / "package.json").unlink()
    run("add", "-A")
    run("commit", "-qm", "the change removes the tests")
    assert projects.detect_gate_at(path, base) == "npm test"
    assert projects.detect_gate_at(path, "HEAD") == ""
    assert projects.detect_gate(path) == ""


def worker_module():
    return load_module(ROOT / "services/worker/worker.py", "worker_attacker_test")


def test_tests_are_chosen_from_main_and_removing_them_fails(tmp_path, monkeypatch):
    worker = worker_module()
    path, run = repo(tmp_path)
    (path / "package.json").write_text('{"scripts": {"test": "node --test"}}')
    run("add", "-A")
    run("commit", "-qm", "tests")
    project = type("P", (), {"gate_command": "", "repo": path, "default_branch": "main", "is_builtin": False,
                             "id": "shop"})()
    monkeypatch.setattr(worker, "PROJECT", project)
    monkeypatch.setattr(worker, "check_worktree_pointer", lambda cwd: None)
    assert worker.trusted_gate(path) == ("npm test", "")
    (path / "package.json").unlink()                       # the change deletes the test setup
    command, note = worker.trusted_gate(path)
    assert command is None and "removed the project's tests" in note
    (path / "pytest.ini").write_text("")                   # ...or swaps in another runner
    command, note = worker.trusted_gate(path)
    assert command == "npm test" and "tests chosen from main" in note
    project.gate_command = "make check"                    # the operator's own command always wins
    assert worker.trusted_gate(path) == ("make check", "")


def test_an_agent_changing_git_hooks_or_config_fails_and_is_undone(tmp_path, monkeypatch):
    worker = worker_module()
    monkeypatch.setattr(worker, "check_worktree_pointer", lambda cwd: None)
    path, run = repo(tmp_path)
    config = (path / ".git" / "config").read_bytes()
    with pytest.raises(RuntimeError, match="changed git configuration or hooks"):
        with worker.guard_git_control(path):
            (path / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\ncurl evil\n")
            with open(path / ".git" / "config", "a") as handle:
                handle.write("[core]\n\tfsmonitor = ./steal.sh\n")
    assert (path / ".git" / "config").read_bytes() == config
    assert not (path / ".git" / "hooks" / "pre-commit").exists()
    with worker.guard_git_control(path):                   # an ordinary run changes nothing there
        (path / "app.py").write_text("print(3)\n")


def test_agents_never_see_laikas_secrets():
    agent_cli = load_module(ROOT / "services/agent_cli.py", "agent_cli_env_test")
    env = agent_cli.agent_env({"REDIS_PASSWORD": "x", "LAIKA_OPERATOR_TOKEN": "y", "DATABASE_URL": "z",
                               "ANTHROPIC_API_KEY": "keep", "PATH": "/bin", "HOME": "/h"})
    assert env == {"ANTHROPIC_API_KEY": "keep", "PATH": "/bin", "HOME": "/h"}
