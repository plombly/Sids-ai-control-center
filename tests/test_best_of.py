"""Best-of-N builds: a failed build is rebuilt by both providers in parallel."""

import json
import subprocess

import pytest

from sid_testing import MemoryRedis, ROOT, load_module


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    (main / "app.py").write_text("base\n")
    git(main, "add", ".")
    git(main, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return main


@pytest.fixture
def worker(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("REVIEW_ASPECTS", "spec")
    module = load_module(ROOT / "services/worker/worker.py")
    module.redis = MemoryRedis()
    module.redis.records["sid:jobs:b1"] = {"id": "b1", "role": "builder", "build_attempt": "2"}
    module.heartbeat = lambda status="idle": None
    # A build "passes the gate" when app.py says OK.
    module.run_tests = lambda wt: ("OK" in (wt / "app.py").read_text(), "")
    return module


def builders(worker, primary=None, alt=None, primary_rc=0, alt_ok=True, new_file=None):
    def run_agent(job, worktree, log_path):
        if primary is not None:
            (worktree / "app.py").write_text(primary)
        return primary_rc, 1.0

    def run_alternate(job, provider, worktree, log_path, timeout):
        if alt is not None:
            (worktree / "app.py").write_text(alt)
        if new_file:
            (worktree / new_file).write_text("added by alt\n")
        return alt_ok, 2.0, "0.1234", "sonnet"

    worker.run_agent = run_agent
    worker.run_alternate_builder = run_alternate


def build(worker):
    _, worktree = worker.create_worktree("b1")
    result = worker.run_best_of({"id": "b1", "role": "builder", "prompt": "p"},
                                worktree, worktree.parent / "b1.jsonl")
    report = json.loads(worker.redis.records["sid:jobs:b1"]["best_of"])
    return result, worktree, report


def test_smaller_passing_alternate_wins_and_lands_in_the_normal_worktree(worker, tmp_path):
    builders(worker, primary="OK\n" + "noise\n" * 20, alt="OK\n", new_file="helper.py")
    (rc, _), worktree, report = build(worker)
    assert rc == 0 and report["chosen"] == "alt"
    assert (worktree / "app.py").read_text() == "OK\n"
    assert (worktree / "helper.py").read_text() == "added by alt\n", "new files carried over"
    assert git(worktree, "diff", "--cached", "--name-only") == "", "left unstaged like a builder"
    assert worker.redis.records["sid:jobs:b1"]["provider"] == "codex"
    assert report["alt"]["provider"] == "codex"
    assert not (tmp_path / "worktrees" / "job-b1-alt").exists(), "alternate worktree removed"
    assert "sid/job-b1-alt" not in git(worktree, "branch", "--list")


def test_primary_kept_when_it_is_no_larger(worker):
    builders(worker, primary="OK\n", alt="OK\nmore\n")
    (rc, _), worktree, report = build(worker)
    assert report["chosen"] == "primary" and rc == 0
    assert (worktree / "app.py").read_text() == "OK\n"


def test_alternate_wins_when_primary_fails_its_tests(worker):
    builders(worker, primary="broken\n", alt="OK\n")
    _, worktree, report = build(worker)
    assert report["chosen"] == "alt" and report["primary"]["tests"] is False
    assert (worktree / "app.py").read_text() == "OK\n"


def test_alternate_wins_when_primary_agent_fails(worker):
    builders(worker, primary="half\n", alt="OK\n", primary_rc=1)
    (rc, _), worktree, report = build(worker)
    assert rc == 0 and report["chosen"] == "alt"
    assert (worktree / "app.py").read_text() == "OK\n"


def test_both_fail_returns_the_primary_result(worker):
    builders(worker, primary="broken\n", alt=None, alt_ok=False)
    (rc, _), worktree, report = build(worker)
    assert report["chosen"] == "primary" and rc == 0
    assert (worktree / "app.py").read_text() == "broken\n", "the gate then fails it as usual"
    assert "error" in report["alt"]


def test_skipped_when_claude_is_at_capacity(worker, monkeypatch):
    monkeypatch.setenv("BEST_OF_ALT_PROVIDER", "claude")  # opt-in only
    monkeypatch.setenv("CLAUDE_MAX_CONCURRENT", "1")
    worker.agent_cli.acquire_claude_slot(worker.redis, "job:busy", 1000)
    worker.agent_cli.wait_for_claude_slot = lambda *a, **k: False
    calls = []
    worker.run_agent = lambda job, wt, log: calls.append(job["id"]) or (0, 1.0)
    _, worktree = worker.create_worktree("b1")
    worker.run_best_of({"id": "b1", "role": "builder", "prompt": "p"}, worktree, worktree.parent / "l")
    assert calls == ["b1"]
    assert "skipped" in json.loads(worker.redis.records["sid:jobs:b1"]["best_of"])


@pytest.mark.parametrize("attempt,role,setting,enabled", [
    ("1", "builder", "2", False), ("2", "builder", "2", True), ("3", "builder", "2", True),
    ("2", "repair", "2", False), ("5", "builder", "0", False), ("1", "builder", "1", True),
])
def test_when_best_of_applies(worker, attempt, role, setting, enabled):
    worker.BEST_OF_FROM_ATTEMPT = int(setting)
    worker.redis.records["sid:jobs:b1"]["build_attempt"] = attempt
    assert worker.best_of_enabled({"id": "b1", "role": role}) is enabled


def test_default_second_build_is_codex_and_never_claude(repo, tmp_path, monkeypatch):
    # Heavy building stays on Codex (the operator's routing intent).
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.delenv("BEST_OF_ALT_PROVIDER", raising=False)
    module = load_module(ROOT / "services/worker/worker.py")
    module.redis = MemoryRedis()
    module.redis.records["sid:jobs:b1"] = {"id": "b1", "role": "builder", "build_attempt": "2"}
    module.heartbeat = lambda status="idle": None
    module.run_tests = lambda wt: (True, "")
    module.run_agent = lambda job, wt, log: (0, 1.0)
    prompts = []
    module.run_codex = lambda job, wt, log: prompts.append(job["prompt"]) or (0, 1.0)
    module.agent_cli.run_claude = lambda *a, **k: pytest.fail("Claude must not build")
    module.agent_cli.wait_for_claude_slot = lambda *a, **k: pytest.fail("no Claude slot needed")
    _, worktree = module.create_worktree("b1")
    module.run_best_of({"id": "b1", "role": "builder", "prompt": "task"}, worktree, worktree.parent / "l")
    assert len(prompts) == 1 and "independent second attempt" in prompts[0]
    assert json.loads(module.redis.records["sid:jobs:b1"]["best_of"])["alt"]["provider"] == "codex"
