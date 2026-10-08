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
