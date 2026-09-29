#!/usr/bin/env python3
"""Fast, no-network regression checks for SID workflow control code."""

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FakeRedisClient:
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
    }
    reviewer = {
        "role": "reviewer",
        "builder_job_id": "builder123",
        "status": "review_complete",
        "review_verdict": "pass",
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


def main():
    tests = [
        test_approval_review_gate,
        test_worker_noop_precedes_review_dispatch,
        test_manual_review_duplicate_guard,
    ]
    for test in tests:
        test()
        print(f"[ PASS ] {test.__name__}")
    print(f"WORKFLOW SELF-TEST: PASSED ({len(tests)} checks)")


if __name__ == "__main__":
    main()
