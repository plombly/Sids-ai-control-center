"""Merge queue: approve a change once, merge it when it is fresh and verified."""

import json

import pytest

from laika_testing import INTEGRATED, JOB, ROOT, MemoryRedis, load_module, make_builder

QUEUE = "laika:merge-queue"
SOURCES = '["s1","s2"]'
H0, H1 = "0" * 40, "1" * 40


def ready_builder(job_review, job_id=JOB, base=H0, candidate=INTEGRATED, **fields):
    record = {
        "id": job_id, "role": "builder", "status": "awaiting_review",
        "review_status": "complete", "review_verdict": "pass",
        "integration_status": "passed", "integration_base_commit": base,
        "integrated_candidate_commit": candidate, "reviewed_commit": candidate,
        "source_candidate_commits": SOURCES, **fields,
    }
    job_review.r.records[f"laika:jobs:{job_id}"] = record
    return record


def refused(call, contains):
    with pytest.raises(SystemExit) as caught:
        call()
    assert contains.lower() in caught.value.message.lower()


# --- queue_approval --------------------------------------------------------------

def test_queue_approval_records_the_approved_change(job_review):
    ready_builder(job_review)
    job_review.queue_approval(JOB, INTEGRATED)
    job_review.queue_approval(JOB, INTEGRATED)  # idempotent
    b = job_review.r.records[f"laika:jobs:{JOB}"]
    assert json.loads(b["approval_intent_sources"]) == ["s1", "s2"]
    assert b["approval_intent_candidate"] == INTEGRATED
    assert b["merge_queue_state"] == "queued"
    assert job_review.r.values[QUEUE] == [JOB]


@pytest.mark.parametrize("fields,reason", [
    ({"status": "needs_human"}, "expected 'awaiting_review'"),
    ({"review_verdict": "changes_required"}, "review has not passed"),
    ({"review_status": "running"}, "review has not passed"),
    ({"integration_status": "failed"}, "integration did not pass"),
    ({"reviewed_commit": "d" * 40}, "review did not target"),
    ({"source_candidate_commits": "[]"}, "no source candidate commits"),
])
def test_queue_approval_refusals(job_review, fields, reason):
    ready_builder(job_review, **fields)
    refused(lambda: job_review.queue_approval(JOB, INTEGRATED), reason)
    assert QUEUE not in job_review.r.values


def test_queue_approval_binds_to_the_candidate_the_human_saw(job_review):
    ready_builder(job_review)
    refused(lambda: job_review.queue_approval(JOB, "d" * 40), "is not the integrated candidate")


def test_dequeue_and_reject_remove_from_queue(job_review, fake_git):
    ready_builder(job_review)
    job_review.queue_approval(JOB, INTEGRATED)
    job_review.dequeue_approval(JOB)
    assert job_review.r.values[QUEUE] == []
    assert "approval_intent_sources" not in job_review.r.records[f"laika:jobs:{JOB}"]
    make_builder(job_review)  # reject needs worktree/branch fields
    job_review.r.records[f"laika:jobs:{JOB}"].update(ready_builder(job_review))
    job_review.r.records[f"laika:jobs:{JOB}"]["worktree"] = str(job_review.WORKTREE_ROOT / f"job-{JOB}")
    job_review.r.records[f"laika:jobs:{JOB}"]["branch"] = f"laika/job-{JOB}"
    job_review.queue_approval(JOB, INTEGRATED)
    job_review.reject(JOB)
    assert job_review.r.values[QUEUE] == []


# --- readiness --------------------------------------------------------------------

def intent(job_review, **fields):
    b = ready_builder(job_review, **fields)
    job_review.queue_approval(JOB, INTEGRATED)
    return b


@pytest.mark.parametrize("change,state,reason", [
    ({}, "ready", ""),
    ({"integration_base_commit": H1}, "wait", "stale"),
    ({"review_status": "queued"}, "wait", "in progress"),
    ({"integration_status": "running"}, "wait", "in progress"),
    ({"reviewed_commit": "e" * 40}, "wait", "in progress"),
    ({"source_candidate_commits": '["s1","s2","s3"]'}, "invalid", "differs from what was approved"),
    ({"review_verdict": "changes_required"}, "invalid", "requires changes"),
    ({"status": "merged"}, "invalid", "not awaiting review"),
])
def test_merge_readiness(job_review, change, state, reason):
    b = intent(job_review)
    b.update(change)
    got_state, got_reason = job_review.merge_readiness(b, H0)
    assert got_state == state and reason in got_reason


# --- process_merge_queue ---------------------------------------------------------------

@pytest.fixture
def queue_env(job_review, fake_git):
    """approve() stubbed to a successful merge that moves main to H1."""
    fake_git.main_head = H0
    merged = []

    def approve(job_id, expected_candidate=None):
        record = job_review.r.records[f"laika:jobs:{job_id}"]
        assert expected_candidate == record["integrated_candidate_commit"]
        record["status"] = "merged"
        merged.append(job_id)
        fake_git.main_head = H1

    job_review.approve = approve
    return merged


def test_ready_job_merges_through_approve_with_its_current_candidate(job_review, queue_env):
    intent(job_review)
    assert job_review.process_merge_queue() == [JOB]
    assert queue_env == [JOB]
    assert job_review.r.values[QUEUE] == []
    b = job_review.r.records[f"laika:jobs:{JOB}"]
    assert (b["merge_queue_state"], b["merged_via"]) == ("merged", "merge-queue")
    assert job_review.r.values["laika:main-head"] == H0


def test_at_most_one_merge_per_pass(job_review, queue_env):
    ready_builder(job_review, "a")
    job_review.queue_approval("a", INTEGRATED)
    ready_builder(job_review, "b")
    job_review.queue_approval("b", INTEGRATED)
    assert job_review.process_merge_queue() == ["a"]
    assert job_review.r.values[QUEUE] == ["b"]
    # main moved: b is now stale and waits for re-integration
    assert job_review.process_merge_queue() == []
    assert job_review.r.records["laika:jobs:b"]["merge_queue_state"] == "waiting"
    assert "stale" in job_review.r.records["laika:jobs:b"]["merge_queue_reason"]


def test_ready_job_behind_a_waiting_one_still_merges(job_review, queue_env):
    ready_builder(job_review, "a", base=H1)  # stale
    job_review.queue_approval("a", INTEGRATED)
    ready_builder(job_review, "b")
    job_review.queue_approval("b", INTEGRATED)
    assert job_review.process_merge_queue() == ["b"]


def test_invalid_intent_is_dropped_with_reason(job_review, queue_env):
    b = intent(job_review)
    b["source_candidate_commits"] = '["s1","s2","repair"]'
    assert job_review.process_merge_queue() == []
    assert queue_env == []
    assert job_review.r.values[QUEUE] == []
    assert b["merge_queue_state"] == "invalid"
    assert "approve again" in b["merge_queue_reason"]


def test_transient_refusal_keeps_the_job_queued(job_review, queue_env):
    intent(job_review)

    def busy(job_id, expected_candidate=None):
        job_review.fail("another approval is already advancing main")

    job_review.approve = busy
    assert job_review.process_merge_queue() == []
    assert job_review.r.values[QUEUE] == [JOB]


def test_hard_refusal_drops_the_job(job_review, queue_env):
    b = intent(job_review)

    def refuse(job_id, expected_candidate=None):
        job_review.fail("integrated branch changed after review")

    job_review.approve = refuse
    job_review.process_merge_queue()
    assert job_review.r.values[QUEUE] == []
    assert "integrated branch changed" in b["merge_queue_reason"]


def test_cli_queue_requires_full_candidate(job_review, monkeypatch):
    monkeypatch.setattr("sys.argv", ["job-review.py", "queue", JOB])
    refused(job_review.main, "requires --candidate")
    monkeypatch.setattr("sys.argv", ["job-review.py", "queue", JOB, "--candidate", "abc"])
    refused(job_review.main, "full 40-character")


# --- orchestrator re-integrates stale queued approvals -----------------------------------

@pytest.fixture
def orch():
    module = load_module(ROOT / "services/orchestrator/orchestrator.py")
    module.r = MemoryRedis()
    module.r.records["laika:workers:w1"] = {"id": "w1", "job_id": ""}
    return module


def queued_builder(orch, job_id="b1", base=H0, **fields):
    orch.r.records[f"laika:jobs:{job_id}"] = {
        "id": job_id, "role": "builder", "status": "awaiting_review",
        "integration_status": "passed", "review_status": "complete", "review_verdict": "pass",
        "integration_base_commit": base, "integrated_candidate_commit": INTEGRATED,
        "reviewed_commit": INTEGRATED, "source_candidate_commits": SOURCES,
        "review_job_id": "rv1", **fields}
    orch.r.rpush(QUEUE, job_id)
    return orch.r.records[f"laika:jobs:{job_id}"]


def integrate_jobs(orch):
    return [json.loads(raw) for raw in orch.r.values.get("laika:jobs", [])]


def test_stale_queued_approval_is_reintegrated_on_new_main(orch):
    b = queued_builder(orch)
    orch.refresh_queued_candidates(head=H1)
    [queued] = integrate_jobs(orch)
    assert queued["role"] == "integrate" and queued["target_builder_id"] == "b1"
    assert b["stale_reintegrated_for"] == H1
    assert b["source_candidate_commits"] == SOURCES, "same change, new base"
    for gone in ("integrated_candidate_commit", "review_verdict", "integration_base_commit"):
        assert gone not in b
    orch.refresh_queued_candidates(head=H1)
    assert len(integrate_jobs(orch)) == 1, "once per main commit"


def test_fresh_unqueued_or_busy_candidates_are_left_alone(orch):
    queued_builder(orch, "fresh", base=H1)
    orch.r.records["laika:jobs:unqueued"] = {"id": "unqueued", "role": "builder", "status": "awaiting_review",
                                           "integration_status": "passed", "review_status": "complete",
                                           "review_verdict": "pass", "integration_base_commit": H0}
    queued_builder(orch, "busy")
    orch.r.records["laika:workers:w1"]["job_id"] = "rv1"  # its review is running
    orch.refresh_queued_candidates(head=H1)
    assert integrate_jobs(orch) == []


# --- end to end: two approvals, one after the other ------------------------------------

def test_two_queued_approvals_both_merge_across_a_main_move(job_review, queue_env, orch):
    orch.r = job_review.r  # one Redis for host authority and orchestrator
    orch.r.records["laika:workers:w1"] = {"id": "w1", "job_id": ""}
    ready_builder(job_review, "a")
    job_review.queue_approval("a", INTEGRATED)
    ready_builder(job_review, "b", candidate="c" * 39 + "b")
    job_review.queue_approval("b", "c" * 39 + "b")

    assert job_review.process_merge_queue() == ["a"]          # main -> H1, b stale
    orch.refresh_queued_candidates(head=H1)                   # b re-integrated
    assert job_review.process_merge_queue() == []           # b waits for review
    b = job_review.r.records["laika:jobs:b"]
    # the worker re-integrates the same sources on H1 and a fresh review passes:
    b.update(integration_status="passed", integration_base_commit=H1,
             integrated_candidate_commit="f" * 40, reviewed_commit="f" * 40,
             review_status="complete", review_verdict="pass")
    assert job_review.process_merge_queue() == ["b"]
    assert queue_env == ["a", "b"]
    assert job_review.r.values[QUEUE] == []


# --- operator service and API accept the new actions -----------------------------------

def test_operator_dispatches_queue_actions(job_review):
    op = load_module(ROOT / "services/operator/laika_operator.py")
    seen = []
    job_review.queue_approval = lambda job_id, candidate: seen.append(("queue", job_id, candidate))
    job_review.dequeue_approval = lambda job_id: seen.append(("dequeue", job_id))
    op.job_review = job_review
    op.call_action({"action": "queue_approve", "job_id": JOB, "expected_candidate": INTEGRATED})
    op.call_action({"action": "dequeue_approve", "job_id": JOB})
    assert seen == [("queue", JOB, INTEGRATED), ("dequeue", JOB)]
    fields = {"action": "queue_approve", "job_id": JOB, "expected_status": "awaiting_review"}
    op.ALLOWED_ACTIONS = frozenset(op.ACTIONS)
    with pytest.raises(op.Invalid, match="full 40-character"):
        op.validate(fields, "9999999999999-0", 9999999999.0)
