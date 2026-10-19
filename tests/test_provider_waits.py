"""Builds that wait for a provider (or a lost worker) keep their attempt."""

import json

import pytest

from laika_testing import MemoryRedis, ROOT, load_module


@pytest.fixture
def orch():
    module = load_module(ROOT / "services/orchestrator/orchestrator.py")
    module.r = MemoryRedis()
    return module


def deferred(orch, job_id, **fields):
    orch.r.records[f"laika:jobs:{job_id}"] = {
        "id": job_id, "role": "builder", "status": "deferred", "goal_id": "g1", "prompt": "Add a cart",
        "build_attempt": "1", "defer_kind": "provider", "defer_provider": "codex", "deferrals": "1",
        "defer_reason": "codex quota: usage limit", **fields}
    return orch.r.records[f"laika:jobs:{job_id}"]


def queued(orch):
    return [json.loads(raw)["id"] for raw in orch.r.values.get("laika:jobs", [])]


def test_a_provider_wait_resumes_only_after_the_hold_ends_with_the_same_attempt(orch):
    import provider_health
    deferred(orch, "b1")
    provider_health.hold(orch.r, "codex", provider_health.Failure("quota", "usage limit"))
    orch.resume_deferred_jobs()
    assert orch.r.records["laika:jobs:b1"]["status"] == "deferred" and queued(orch) == []
    provider_health.release(orch.r, "codex")
    orch.resume_deferred_jobs()
    orch.resume_deferred_jobs()                                   # idempotent: queued once
    job = orch.r.records["laika:jobs:b1"]
    assert job["status"] == "queued" and job["build_attempt"] == "1" and job["prompt"] == "Add a cart"
    assert "defer_reason" not in job and job["deferrals"] == "1"
    assert queued(orch) == ["b1"]


def test_too_many_provider_waits_become_an_ordinary_failure(orch):
    deferred(orch, "b2", deferrals=str(orch.MAX_PROVIDER_DEFERRALS + 1))
    orch.resume_deferred_jobs()
    job = orch.r.records["laika:jobs:b2"]
    assert job["status"] == "failed" and "waited for codex" in job["error"]


def test_deferred_jobs_keep_their_files_and_count_as_remaining(orch):
    assert "deferred" in orch.SCOPE_HOLDING_STATES and "deferred" in orch.REMAINING_STATES
    assert "deferred" not in orch.TERMINAL_FAILURE_STATES


def test_codex_limit_holds_codex_and_raises_a_provider_wait(tmp_path, monkeypatch):
    worker = load_module(ROOT / "services/worker/worker.py", "worker_provider_wait")
    worker.redis = MemoryRedis()
    log = tmp_path / "b.jsonl"

    def failing_codex(job, worktree, log_path, account=None):
        log_path.write_text(json.dumps({"type": "turn.failed", "error": {
            "message": "You've hit your usage limit. Try again in 1 hour."}}) + "\n")
        return 1, 2.0
    monkeypatch.setattr(worker, "run_codex", failing_codex)
    with pytest.raises(worker.ProviderWait) as wait:
        worker.codex_or_wait({"id": "b1", "role": "builder"}, tmp_path, log)
    assert wait.value.provider == "codex" and wait.value.failure.kind == "quota"
    assert worker.provider_health.held(worker.redis, "codex")
    with pytest.raises(worker.ProviderWait):                       # held: not even started
        worker.codex_or_wait({"id": "b2", "role": "builder"}, tmp_path, log)

    def broken_build(job, worktree, log_path, account=None):
        log_path.write_text('{"type":"turn.failed","error":{"message":"tests failed"}}\n')
        return 1, 1.0
    worker.provider_health.release(worker.redis, "codex")
    monkeypatch.setattr(worker, "run_codex", broken_build)
    assert worker.codex_or_wait({"id": "b3", "role": "builder"}, tmp_path, log) == (1, 1.0)   # an ordinary failure


def test_claude_usage_readings_are_kept_and_a_full_window_holds_claude(tmp_path):
    agent_cli = load_module(ROOT / "services/agent_cli.py", "agent_cli_usage")
    r = MemoryRedis()
    event = lambda five, week: json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": five, "resetsAt": 2000000000},
                                                "seven_day": {"utilization": week, "resetsAt": 2000500000}}}})
    log = tmp_path / "c.json"
    log.write_text(event(0.2, 0.5) + "\n" + event(0.4, 0.6) + "\n")
    reading = agent_cli.record_rate_limits(r, log, now=1999990000)
    assert reading["windows"]["five_hour"]["percent"] == 40.0
    assert json.loads(r.values["laika:provider-usage:claude"])["windows"]["seven_day"]["percent"] == 60.0
    assert not agent_cli.claude_cooling_down(r)
    log.write_text(event(0.5, 0.97) + "\n")
    agent_cli.record_rate_limits(r, log, now=1999990000)
    assert agent_cli.claude_cooling_down(r)
    assert "7-day window at 97%" in r.values[agent_cli.COOLDOWN_KEY]
    assert agent_cli.record_rate_limits(r, tmp_path / "missing.json") is None      # fails open


def test_a_paused_server_claims_no_new_job():
    worker = load_module(ROOT / "services/worker/worker.py", "worker_pause_test")
    worker.redis = MemoryRedis()
    assert worker.pause_reason() is None
    worker.redis.values["laika:pause"] = '{"by": "admin"}'
    assert worker.pause_reason() == "paused"


def test_a_review_or_repair_that_no_provider_can_serve_waits_without_using_an_attempt():
    import provider_health
    worker = load_module(ROOT / "services/worker/worker.py", "worker_review_wait_test")
    worker.redis = MemoryRedis()
    worker.redis.records["laika:jobs:b1"] = {"id": "b1", "repair_attempts": "2"}
    wait = worker.ProviderWait("codex", provider_health.Failure("quota", "usage limit"))
    review = worker.provider_wait_fields("b1", wait, "review", "rv1")
    assert review["review_wait_provider"] == "codex" and review["review_waits"] == "1"
    repair = worker.provider_wait_fields("b1", wait, "repair", "rp1")
    assert repair["repair_status"] == "waiting" and repair["repair_attempts"] == "1"   # the attempt comes back
    assert worker.provider_wait_fields("b1", RuntimeError("tests failed"), "repair", "rp1") == {}
    worker.redis.records["laika:jobs:b1"]["review_waits"] = str(worker.MAX_PROVIDER_DEFERRALS)
    assert worker.provider_wait_fields("b1", wait, "review", "rv1") == {}             # bounded


def test_a_waiting_repair_is_dispatched_only_once_the_provider_is_back(orch):
    import provider_health
    orch.r.records["laika:jobs:b1"] = {
        "id": "b1", "role": "builder", "status": "awaiting_review", "review_status": "complete",
        "review_verdict": "changes_required", "repair_attempts": "0", "repair_status": "waiting",
        "repair_wait_provider": "codex", "review_job_id": "r1", "worktree": "/w/job-b1", "prompt": "x"}
    provider_health.hold(orch.r, "codex", provider_health.Failure("quota", "usage limit"))
    orch.queue_repairs()
    assert not orch.r.values.get("laika:jobs") and orch.r.records["laika:jobs:b1"]["repair_status"] == "waiting"


def test_a_waiting_review_resumes_without_using_a_recovery(orch):
    import provider_health
    orch.r.records["laika:jobs:b1"] = {
        "id": "b1", "role": "builder", "status": "awaiting_review", "review_status": "failed",
        "review_wait_provider": "codex", "review_waits": "1", "review_job_id": "rv1", "goal_id": "g1"}
    provider_health.hold(orch.r, "codex", provider_health.Failure("quota", "usage limit"))
    orch.recover_stalled_reviews(1000)
    assert queued(orch) == []
    provider_health.release(orch.r, "codex")
    orch.recover_stalled_reviews(1001)
    orch.recover_stalled_reviews(1002)                                   # once
    builder = orch.r.records["laika:jobs:b1"]
    assert len(queued(orch)) == 1 and builder["review_wait_provider"] == "" and "review_recoveries" not in builder
