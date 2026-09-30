"""The Redis password lookup shared by SID's host programs."""

import sys

from sid_testing import ROOT

sys.path.insert(0, str(ROOT / "services"))
import sid_redis  # noqa: E402


def test_password_from_env_then_file_then_none(monkeypatch, tmp_path):
    env_file = tmp_path / "redis.env"
    monkeypatch.setattr(sid_redis, "ENV_FILE", str(env_file))
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    assert sid_redis.password() is None
    env_file.write_text("# SID\nREDIS_PASSWORD=abc123\n")
    assert sid_redis.password() == "abc123"
    monkeypatch.setenv("REDIS_PASSWORD", "fromenv")
    assert sid_redis.password() == "fromenv"


def test_every_host_program_sends_the_password():
    files = ["services/worker/worker.py", "services/orchestrator/orchestrator.py",
             "services/operator/sid_operator.py", "scripts/job-review.py", "scripts/sid-project.py",
             "scripts/sid-watchdog.py", "scripts/prune-sid-data.py", "scripts/submit-goal.py",
             "scripts/submit-job.py", "scripts/submit-review.py", "scripts/goal-status.py",
             "scripts/local-diagnostic.py", "apps/tui/sid-tui.py"]
    for name in files:
        source = (ROOT / name).read_text()
        assert "password=sid_redis.password()" in source, name
    assert 'password=os.environ.get("REDIS_PASSWORD")' in (ROOT / "apps/api/main.py").read_text()
    # redis-cli runs inside the container with its own env, never with the password in argv.
    backup = (ROOT / "scripts/backup-sid.py").read_text()
    assert '"redis-cli", "--rdb"' not in backup and "REDISCLI_AUTH" in backup
