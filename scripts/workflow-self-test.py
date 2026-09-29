#!/usr/bin/env python3
"""Fast, no-network regression checks for SID workflow control code."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FakeRedisClient:
    def set(self, *args, **kwargs):
        return True

    def eval(self, *args, **kwargs):
        return 0

    @classmethod
    def from_url(cls, *args, **kwargs):
        return cls()


fake_redis = types.ModuleType("redis")
fake_redis.Redis = FakeRedisClient
sys.modules.setdefault("redis", fake_redis)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expect_exit(call, contains):
    try:
        call()
    except SystemExit as exc:
        if exc.code != 1:
            raise AssertionError(f"expected exit 1, got {exc.code}") from exc
    else:
        raise AssertionError("expected SystemExit")


def test_approval_review_gate():
    module = load("sid_job_review_test", ROOT / "scripts/job-review.py")

    builder = {
        "status": "awaiting_review",
        "review_status": "complete",
        "review_verdict": "pass",
        "review_job_id": "review123",
        "candidate_commit": "abc123",
        "integrated_candidate_commit": "abc123",
        "integration_base_commit": "base123",
        "integration_status": "passed",
        "reviewed_commit": "abc123",
    }
    reviewer = {
        "role": "reviewer",
        "builder_job_id": "builder123",
        "status": "review_complete",
        "review_verdict": "pass",
        "candidate_commit": "abc123",
        "reviewed_commit": "abc123",
    }

    records = {"builder123": builder, "review123": reviewer}
    module.job_record = lambda job_id: records[job_id]

    reached_clean_check = []

    def stop_at_clean_check():
        reached_clean_check.append(True)
        raise RuntimeError("STOP_AFTER_REVIEW_GATE")

    module.ensure_main_clean = stop_at_clean_check

    try:
        module.approve("builder123")
    except RuntimeError as exc:
        assert str(exc) == "STOP_AFTER_REVIEW_GATE"
    else:
        raise AssertionError("valid review did not reach main-clean check")

    assert reached_clean_check

    builder["review_verdict"] = "changes_required"
    expect_exit(lambda: module.approve("builder123"), "verdict")
    builder["review_verdict"] = "pass"

    reviewer["builder_job_id"] = "someone_else"
    expect_exit(lambda: module.approve("builder123"), "another builder")


def test_worker_noop_precedes_review_dispatch():
    source = (ROOT / "services/worker/worker.py").read_text()
    noop = source.index('if not diff.strip():')
    dispatch = source.index('review_job_id, queued = queue_review_job(job_id)', noop)
    assert noop < dispatch
    assert '"status": "completed_no_changes"' in source[noop:dispatch]


def test_manual_review_duplicate_guard():
    source = (ROOT / "scripts/submit-review.py").read_text()
    assert 'hsetnx(builder_key, "review_job_id", job_id)' in source



def test_immutable_candidate_contract():
    worker = (ROOT / "services/worker/worker.py").read_text()
    approval = (ROOT / "scripts/job-review.py").read_text()
    manual = (ROOT / "scripts/submit-review.py").read_text()

    assert '"candidate_commit": candidate_commit' in worker
    assert '"reviewed_commit": candidate_commit' in worker
    assert 'f"Apply SID job {job_id}"' in worker
    assert 'Builder worktree changed after candidate commit' in worker
    assert 'Candidate changed during read-only review' in worker

    assert 'data.get("reviewed_commit") != integrated_commit' in approval
    assert 'review.get("reviewed_commit") != integrated_commit' in approval
    assert 'integrated worktree HEAD changed after review' in approval
    assert 'integrated branch changed after review' in approval

    assert 'candidate_commit = builder.get("integrated_candidate_commit")' in manual
    assert '"candidate_commit": candidate_commit' in manual


def test_parallel_integration_contract():
    worker = (ROOT / "services/worker/worker.py").read_text()
    approval = (ROOT / "scripts/job-review.py").read_text()
    manual = (ROOT / "scripts/submit-review.py").read_text()

    assert "source_candidate_commits" in worker
    assert "integration_base_commit" in worker
    assert "integrated_candidate_commit" in worker
    assert "integration_status" in worker
    assert "integration_result" in worker
    assert '"--ff-only"' in approval
    assert "stale main" in approval
    assert "safe_integration_worktree" in approval
    assert 'builder.get("integrated_candidate_commit")' in manual


def test_integration_and_review_duplicate_guards():
    worker = (ROOT / "services/worker/worker.py").read_text()
    submitter = (ROOT / "scripts/submit-review.py").read_text()

    # Integration must have an atomic per-job ownership reservation, not
    # merely an optimistic integration_status check.
    assert 'lock_key = f"sid:integration-lock:{job_id}"' in worker
    assert 'redis.set(lock_key, lock_owner, nx=True, ex=lock_ttl)' in worker
    assert 'lock_ttl = max(MAX_RUNTIME + 300, 7200)' in worker
    assert 'if not acquired:' in worker
    assert 'return _prepare_integration_locked(job_id, key)' in worker
    assert "redis.eval(" in worker
    assert "redis.call('get', KEYS[1]) == ARGV[1]" in worker
    assert "redis.call('del', KEYS[1])" in worker

    # Repairs preserve the ordered source chain instead of replacing it
    # with a dependent repair commit that cannot stand alone on main.
    assert 'raw_sources = builder.get("source_candidate_commits", "")' in worker
    assert 'sources.append(candidate_after)' in worker
    assert '"source_candidate_commits": json.dumps(sources' in worker

    # Review reservation remains atomic as well.
    assert 'hsetnx(builder_key, "review_job_id", job_id)' in submitter



class MemoryRedis:
    def __init__(self, records=None):
        self.records = records or {}
        self.values = {}

    def hgetall(self, key):
        return dict(self.records.get(key, {}))

    def hset(self, key, mapping=None, **kwargs):
        record = self.records.setdefault(key, {})
        if mapping:
            record.update({str(k): str(v) for k, v in mapping.items()})
        return 1

    def hdel(self, key, *fields):
        record = self.records.setdefault(key, {})
        for field in fields:
            record.pop(field, None)
        return len(fields)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def eval(self, script, count, key, owner):
        if self.values.get(key) == owner:
            del self.values[key]
            return 1
        return 0


class Result:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def load_worker_for_behavior(name):
    module = load(name, ROOT / "services/worker/worker.py")
    return module


def test_integration_success_behavior():
    module = load_worker_for_behavior("sid_worker_integration_success")

    job_id = "behavior1"
    key = f"sid:jobs:{job_id}"
    records = {
        key: {
            "candidate_commit": "source1",
            "source_candidate_commits": json.dumps(["source1"]),
        }
    }
    module.redis = MemoryRedis(records)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        integration = root / f"job-{job_id}-integration"
        module.WORKTREE_ROOT = root

        def fake_git(*args, cwd=module.REPO_ROOT, check=True):
            if args == ("status", "--porcelain"):
                return Result("")
            if args == ("rev-parse", "HEAD") and cwd == module.REPO_ROOT:
                return Result("base1\n")
            if args[:3] == ("worktree", "add", "-b"):
                integration.mkdir(parents=True)
                return Result()
            if args[:2] == ("show-ref", "--verify"):
                return Result(returncode=1)
            if args[:2] == ("cherry-pick", "--no-edit"):
                return Result()
            if args == ("rev-parse", "HEAD") and Path(cwd) == integration:
                return Result("integrated1\n")
            raise AssertionError(f"unexpected git call: {args} cwd={cwd}")

        module.run_git = fake_git

        old_run = module.subprocess.run
        module.subprocess.run = lambda *a, **kw: Result("gate ok\n", "", 0)
        try:
            assert module.prepare_integration(job_id) is True
        finally:
            module.subprocess.run = old_run

    data = records[key]
    assert data["integration_status"] == "passed"
    assert data["integration_base_commit"] == "base1"
    assert data["integrated_candidate_commit"] == "integrated1"
    assert data["status"] == "awaiting_review"


def test_integration_conflict_preserves_main_behavior():
    module = load_worker_for_behavior("sid_worker_integration_conflict")

    job_id = "behavior2"
    key = f"sid:jobs:{job_id}"
    records = {
        key: {
            "candidate_commit": "source1",
            "source_candidate_commits": json.dumps(["source1"]),
        }
    }
    module.redis = MemoryRedis(records)

    main_before = "base-conflict"
    main_reads = []

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        integration = root / f"job-{job_id}-integration"
        module.WORKTREE_ROOT = root

        def fake_git(*args, cwd=module.REPO_ROOT, check=True):
            if args == ("status", "--porcelain"):
                return Result("")
            if args == ("rev-parse", "HEAD") and cwd == module.REPO_ROOT:
                main_reads.append(main_before)
                return Result(main_before + "\n")
            if args[:3] == ("worktree", "add", "-b"):
                integration.mkdir(parents=True)
                return Result()
            if args[:2] == ("show-ref", "--verify"):
                return Result(returncode=1)
            if args[:2] == ("cherry-pick", "--no-edit"):
                return Result("", "conflict", 1)
            if args[:2] == ("cherry-pick", "--abort"):
                return Result()
            if args[:2] == ("worktree", "remove"):
                if integration.exists():
                    integration.rmdir()
                return Result()
            if args[:2] == ("branch", "-D"):
                return Result()
            raise AssertionError(f"unexpected git call: {args} cwd={cwd}")

        module.run_git = fake_git
        assert module.prepare_integration(job_id) is False

    assert main_reads == [main_before]
    assert records[key]["integration_status"] == "failed"
    assert "integrated_candidate_commit" not in records[key]


def test_integration_gate_failure_preserves_main_behavior():
    module = load_worker_for_behavior("sid_worker_gate_failure")

    job_id = "behavior3"
    key = f"sid:jobs:{job_id}"
    records = {
        key: {
            "candidate_commit": "source1",
            "source_candidate_commits": json.dumps(["source1"]),
        }
    }
    module.redis = MemoryRedis(records)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        integration = root / f"job-{job_id}-integration"
        module.WORKTREE_ROOT = root

        def fake_git(*args, cwd=module.REPO_ROOT, check=True):
            if args == ("status", "--porcelain"):
                return Result("")
            if args == ("rev-parse", "HEAD") and cwd == module.REPO_ROOT:
                return Result("base-gate\n")
            if args[:3] == ("worktree", "add", "-b"):
                integration.mkdir(parents=True)
                return Result()
            if args[:2] == ("show-ref", "--verify"):
                return Result(returncode=1)
            if args[:2] == ("cherry-pick", "--no-edit"):
                return Result()
            if args == ("rev-parse", "HEAD") and Path(cwd) == integration:
                return Result("integrated-gate\n")
            if args[:2] == ("worktree", "remove"):
                if integration.exists():
                    integration.rmdir()
                return Result()
            if args[:2] == ("branch", "-D"):
                return Result()
            raise AssertionError(f"unexpected git call: {args} cwd={cwd}")

        module.run_git = fake_git
        old_run = module.subprocess.run
        module.subprocess.run = lambda *a, **kw: Result("bad gate", "", 1)
        try:
            assert module.prepare_integration(job_id) is False
        finally:
            module.subprocess.run = old_run

    assert records[key]["integration_status"] == "failed"
    assert records[key]["integration_base_commit"] == "base-gate"
    assert "integrated_candidate_commit" not in records[key]


def test_duplicate_integration_behavior():
    module = load_worker_for_behavior("sid_worker_duplicate_integration")

    fake = MemoryRedis({})
    fake.values["sid:integration-lock:duplicate1"] = "other-worker"
    module.redis = fake

    entered = []
    module._prepare_integration_locked = lambda *a: entered.append(True)

    assert module.prepare_integration("duplicate1") is False
    assert entered == []
    assert fake.values["sid:integration-lock:duplicate1"] == "other-worker"


def approval_fixture(name, main_commit="base1"):
    module = load(name, ROOT / "scripts/job-review.py")

    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    main = root / "main"
    wtroot = root / "worktrees"
    integrated = wtroot / "job-builder123-integration"
    builder_wt = wtroot / "job-builder123"

    main.mkdir()
    integrated.mkdir(parents=True)
    builder_wt.mkdir()

    module.REPO_ROOT = main
    module.WORKTREE_ROOT = wtroot

    builder = {
        "status": "awaiting_review",
        "review_status": "complete",
        "review_verdict": "pass",
        "review_job_id": "review123",
        "candidate_commit": "source1",
        "integrated_candidate_commit": "integrated1",
        "integration_base_commit": "base1",
        "integration_status": "passed",
        "reviewed_commit": "integrated1",
        "integration_worktree": str(integrated),
        "integration_branch": "sid/integration-builder123",
        "worktree": str(builder_wt),
        "branch": "sid/job-builder123",
    }
    reviewer = {
        "role": "reviewer",
        "builder_job_id": "builder123",
        "status": "review_complete",
        "review_verdict": "pass",
        "candidate_commit": "integrated1",
        "reviewed_commit": "integrated1",
    }

    records = {"builder123": builder, "review123": reviewer}
    module.job_record = lambda job_id: records[job_id]

    calls = []

    def fake_git(*args, cwd=module.REPO_ROOT, check=True):
        calls.append((args, Path(cwd)))
        if args == ("status", "--porcelain"):
            return Result("")
        if args == ("rev-parse", "HEAD") and Path(cwd) == main:
            return Result(main_commit + "\n")
        if args == ("rev-parse", "HEAD") and Path(cwd) == integrated:
            return Result("integrated1\n")
        if args == ("status", "--porcelain") and Path(cwd) == integrated:
            return Result("")
        if args == ("rev-parse", "sid/integration-builder123"):
            return Result("integrated1\n")
        if args[:2] == ("merge", "--ff-only"):
            return Result()
        if args[:2] == ("worktree", "remove"):
            return Result()
        if args[:2] in (("branch", "-d"), ("branch", "-D")):
            return Result()
        raise AssertionError(f"unexpected git call: {args} cwd={cwd}")

    module.git = fake_git
    module.r = MemoryRedis({"sid:jobs:builder123": builder})
    return td, module, builder, reviewer, calls


def test_integrated_commit_binding_behavior():
    td, module, builder, reviewer, calls = approval_fixture(
        "sid_approval_binding"
    )
    try:
        builder["reviewed_commit"] = "wrong"
        expect_exit(lambda: module._approve_unlocked("builder123"), "exact integrated")
        assert not any(args[:2] == ("merge", "--ff-only") for args, _ in calls)

        builder["reviewed_commit"] = "integrated1"
        reviewer["candidate_commit"] = "wrong"
        expect_exit(lambda: module._approve_unlocked("builder123"), "candidate")
        assert not any(args[:2] == ("merge", "--ff-only") for args, _ in calls)
    finally:
        td.cleanup()


def test_stale_main_refusal_behavior():
    td, module, builder, reviewer, calls = approval_fixture(
        "sid_approval_stale",
        main_commit="new-main",
    )
    try:
        expect_exit(lambda: module._approve_unlocked("builder123"), "stale main")
        assert not any(args[:2] == ("merge", "--ff-only") for args, _ in calls)
    finally:
        td.cleanup()


def test_exact_approval_behavior():
    td, module, builder, reviewer, calls = approval_fixture(
        "sid_approval_exact"
    )
    try:
        module._approve_unlocked("builder123")

        merges = [
            args for args, cwd in calls
            if args[:2] == ("merge", "--ff-only")
        ]
        assert merges == [
            ("merge", "--ff-only", "sid/integration-builder123")
        ]

        assert module.r.records["sid:jobs:builder123"]["status"] == "merged"
        assert (
            module.r.records["sid:jobs:builder123"]["integrated_candidate_commit"]
            == "integrated1"
        )
    finally:
        td.cleanup()

def main():
    tests = [
        test_approval_review_gate,
        test_worker_noop_precedes_review_dispatch,
        test_manual_review_duplicate_guard,
        test_immutable_candidate_contract,
        test_parallel_integration_contract,
        test_integration_and_review_duplicate_guards,
        test_integration_success_behavior,
        test_integration_conflict_preserves_main_behavior,
        test_integration_gate_failure_preserves_main_behavior,
        test_duplicate_integration_behavior,
        test_integrated_commit_binding_behavior,
        test_stale_main_refusal_behavior,
        test_exact_approval_behavior,
    ]
    for test in tests:
        test()
        print(f"[ PASS ] {test.__name__}")
    print(f"WORKFLOW SELF-TEST: PASSED ({len(tests)} checks)")


if __name__ == "__main__":
    main()
