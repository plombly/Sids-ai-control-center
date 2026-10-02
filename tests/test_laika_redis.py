"""The Redis password lookup shared by LAIka's host programs."""

import sys

from laika_testing import ROOT

sys.path.insert(0, str(ROOT / "services"))
import laika_redis  # noqa: E402


def test_password_from_env_then_file_then_none(monkeypatch, tmp_path):
    env_file = tmp_path / "redis.env"
    monkeypatch.setattr(laika_redis, "ENV_FILE", str(env_file))
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    assert laika_redis.password() is None
    env_file.write_text("# LAIka\nREDIS_PASSWORD=abc123\n")
    assert laika_redis.password() == "abc123"
    monkeypatch.setenv("REDIS_PASSWORD", "fromenv")
    assert laika_redis.password() == "fromenv"


def test_every_host_program_sends_the_password():
    files = ["services/worker/worker.py", "services/orchestrator/orchestrator.py",
             "services/operator/laika_operator.py", "scripts/job-review.py", "scripts/laika-project.py",
             "scripts/laika-watchdog.py", "scripts/laika-prune.py", "scripts/submit-goal.py",
             "scripts/submit-job.py", "scripts/submit-review.py", "scripts/goal-status.py",
             "scripts/local-diagnostic.py", "apps/tui/laika-tui.py"]
    for name in files:
        source = (ROOT / name).read_text()
        assert "password=laika_redis.password()" in source, name
    assert 'password=os.environ.get("REDIS_PASSWORD")' in (ROOT / "apps/api/main.py").read_text()
    # redis-cli runs inside the container with its own env, never with the password in argv.
    backup = (ROOT / "scripts/laika-backup.py").read_text()
    assert '"redis-cli", "--rdb"' not in backup and "REDISCLI_AUTH" in backup


def test_every_entry_point_can_import_laika_redis_on_its_own():
    """Run each program's own imports in a fresh interpreter (tests put
    services/ on sys.path already, which once hid a wrong path)."""
    import subprocess
    programs = ["services/worker/worker.py", "services/orchestrator/orchestrator.py",
                "services/operator/laika_operator.py", "services/apps/laika_apps.py", "scripts/job-review.py",
                "scripts/laika-project.py", "scripts/laika-watchdog.py", "scripts/laika-prune.py",
                "scripts/submit-goal.py", "scripts/submit-job.py", "scripts/submit-review.py",
                "scripts/goal-status.py", "scripts/local-diagnostic.py", "apps/tui/laika-tui.py"]
    for name in programs:
        source = (ROOT / name).read_text()
        line = next(l for l in source.splitlines() if l.startswith("sys.path.insert(0, str(Path(__file__)"))
        code = ("import sys\nfrom pathlib import Path\n__file__ = %r\n%s\nimport laika_redis\n"
                % (str(ROOT / name), line.split("  #")[0]))
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, (name, result.stderr[-300:])
