"""services/worker/worker.py run_git: failures say what git said; install.sh gives a fallback identity."""

import subprocess

from laika_testing import ROOT, load_module


def test_a_failed_git_command_names_gits_own_message(tmp_path, monkeypatch):
    worker = load_module("services/worker/worker.py", "worker_git_test")
    monkeypatch.setattr(worker, "check_worktree_pointer", lambda cwd: None)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    try:
        worker.run_git("rev-parse", "no-such-ref", cwd=tmp_path)
    except subprocess.CalledProcessError as exc:      # still a CalledProcessError for old callers
        message = str(exc)
    assert message.startswith("git rev-parse failed (exit 128)") and "no-such-ref" in message


def test_the_shared_git_config_has_a_fallback_identity():
    text = (ROOT / "install.sh").read_text()
    line = next(line for line in text.splitlines() if '> "$CONF/gitconfig"' in line)
    assert "[user]\\n\\tname = LAIka\\n\\temail = laika@localhost" in line


def test_project_builders_run_tests_with_the_projects_own_dependencies(monkeypatch):
    worker = load_module("services/worker/worker.py", "worker_prompt_test")
    monkeypatch.setattr(worker, "PROJECT", type("P", (), {"is_builtin": False, "id": "shop"})())
    note = worker.python_note("builder")
    assert ".venv/bin/python -m pytest" in note and "install them first" in note
    assert worker.LAIKA_PYTHON not in note
    monkeypatch.setattr(worker, "PROJECT", type("P", (), {"is_builtin": True, "id": "laika"})())
    assert f"{worker.LAIKA_PYTHON} -m pytest" in worker.python_note("builder")


def test_claude_repairs_in_projects_may_install_and_run_the_projects_tests(monkeypatch):
    worker = load_module("services/worker/worker.py", "worker_allowed_test")
    monkeypatch.setattr(worker, "PROJECT", type("P", (), {"is_builtin": False, "id": "shop", "gate_command": ""})())
    allowed = worker.repair_allowed_bash()
    assert "Bash(.venv/bin/python -m pytest *)" in allowed and "Bash(.venv/bin/pip install *)" in allowed
    assert "Bash(npm install)" in allowed and not any(worker.LAIKA_PYTHON in item for item in allowed)
