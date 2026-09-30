"""Behavior of scripts/job-review.py, the host-side operator authority."""

import pytest

from conftest import BASE, INTEGRATED, JOB, make_builder as builder


def refused(call, contains):
    with pytest.raises(SystemExit) as caught:
        call()
    assert caught.value.code == 1
    assert contains.lower() in caught.value.message.lower()
    return caught.value


# --- CLI compatibility -----------------------------------------------------

def test_refused_is_exit_1_with_reason(job_review, capsys):
    error = refused(lambda: job_review.fail("nope"), "nope")
    assert isinstance(error, job_review.Refused)
    assert "ERROR: nope" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    [], ["approve"], ["bogus", JOB], ["reject", JOB, "extra"],
    ["extend", JOB, "1", "2"], ["reopen", JOB, "--candidate", "x"],
])
def test_cli_usage_errors_exit_2(job_review, monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["job-review.py", *argv])
    with pytest.raises(SystemExit) as caught:
        job_review.main()
    assert caught.value.code == 2


def test_cli_rejects_non_alphanumeric_job_id(job_review, monkeypatch):
    monkeypatch.setattr("sys.argv", ["job-review.py", "reject", "../etc"])
    refused(job_review.main, "invalid job id")


# --- reject ------------------------------------------------------------------

@pytest.mark.parametrize("status", sorted(
    {"awaiting_review", "needs_human", "repair_exhausted", "integration_failed"}
))
def test_reject_removes_builder_worktree_and_branch(job_review, fake_git, status):
    builder(job_review, status)
    job_review.reject(JOB)
    record = job_review.r.records[f"sid:jobs:{JOB}"]
    assert record["status"] == "rejected"
    assert record["rejected_at"]
    assert fake_git.ran("worktree", "remove", "--force") == [
        ("worktree", "remove", "--force", str(job_review.WORKTREE_ROOT / f"job-{JOB}"))
    ]
    assert fake_git.ran("branch", "-D") == [("branch", "-D", f"sid/job-{JOB}")]
    assert not fake_git.ran("merge")


def test_reject_also_removes_live_integration_worktree_and_branch(job_review, fake_git):
    integration = job_review.WORKTREE_ROOT / f"job-{JOB}-integration"
    integration.mkdir()
    fake_git.branch_heads[f"sid/integration-{JOB}"] = "c" * 40
    builder(job_review, integration_worktree=str(integration))
    job_review.reject(JOB)
    assert ("worktree", "remove", "--force", str(integration)) in fake_git.ran("worktree")
    assert ("branch", "-D", f"sid/integration-{JOB}") in fake_git.ran("branch")
    assert job_review.r.records[f"sid:jobs:{JOB}"]["status"] == "rejected"


def test_reject_skips_integration_worktree_that_is_already_gone(job_review, fake_git):
    builder(job_review, "needs_human",
            integration_worktree=str(job_review.WORKTREE_ROOT / f"job-{JOB}-integration"))
    job_review.reject(JOB)
    assert fake_git.ran("worktree", "remove", "--force") == [
        ("worktree", "remove", "--force", str(job_review.WORKTREE_ROOT / f"job-{JOB}"))
    ]
    assert job_review.r.records[f"sid:jobs:{JOB}"]["status"] == "rejected"


@pytest.mark.parametrize("status", [
    "queued", "running", "testing", "merged", "rejected", "failed",
    "blocked_failed_dependency", "completed_no_changes",
])
def test_reject_refuses_other_statuses(job_review, fake_git, status):
    builder(job_review, status)
    refused(lambda: job_review.reject(JOB), "job status")
    assert fake_git.calls == []
    assert job_review.r.records[f"sid:jobs:{JOB}"]["status"] == status


def test_reject_refuses_missing_job(job_review, fake_git):
    refused(lambda: job_review.reject(JOB), "job not found")


@pytest.mark.parametrize("field,value,reason", [
    ("worktree", "/opt/sids-ai-command-center", "unexpected worktree path"),
    ("branch", "main", "unexpected branch"),
    ("integration_worktree", "/opt/sids-ai-command-center", "unexpected integration worktree"),
])
def test_reject_refuses_unexpected_paths_before_removing_anything(
        job_review, fake_git, field, value, reason):
    builder(job_review, **{field: value})
    refused(lambda: job_review.reject(JOB), reason)
    assert fake_git.calls == []
    assert job_review.r.records[f"sid:jobs:{JOB}"]["status"] == "awaiting_review"


def test_reject_refuses_when_builder_worktree_is_missing(job_review, fake_git):
    builder(job_review)
    (job_review.WORKTREE_ROOT / f"job-{JOB}").rmdir()
    refused(lambda: job_review.reject(JOB), "worktree does not exist")
    assert fake_git.calls == []


def test_reject_git_failure_leaves_job_unrejected(job_review, fake_git):
    builder(job_review)
    fake_git.failing.add(("branch", "-D"))
    refused(lambda: job_review.reject(JOB), "failed")
    assert job_review.r.records[f"sid:jobs:{JOB}"]["status"] == "awaiting_review"


# --- approve -----------------------------------------------------------------

LOCK = "sid:approval-lock:main"


@pytest.mark.parametrize("expected_candidate", [INTEGRATED, None])
def test_approve_advances_main_to_exact_candidate(
        job_review, fake_git, approvable, expected_candidate):
    job_review.approve(JOB, expected_candidate=expected_candidate)
    assert fake_git.ran("merge") == [("merge", "--ff-only", f"sid/integration-{JOB}")]
    record = job_review.r.records[f"sid:jobs:{JOB}"]
    assert record["status"] == "merged"
    assert record["integrated_candidate_commit"] == INTEGRATED
    assert LOCK not in job_review.r.values


def _set(target, **fields):
    def mutate(job_review, fake_git, builder, reviewer, integration):
        record = builder if target == "builder" else reviewer
        record.update(fields)
    return mutate


def _drop(target, field):
    def mutate(job_review, fake_git, builder, reviewer, integration):
        (builder if target == "builder" else reviewer).pop(field)
    return mutate


def _git(**state):
    def mutate(job_review, fake_git, builder, reviewer, integration):
        for name, value in state.items():
            if name == "branch_head":
                fake_git.branch_heads[f"sid/integration-{JOB}"] = value
            else:
                setattr(fake_git, name, value)
    return mutate


def _remove_integration(job_review, fake_git, builder, reviewer, integration):
    integration.rmdir()


def _drop_reviewer(job_review, fake_git, builder, reviewer, integration):
    del job_review.r.records["sid:jobs:rv1"]


# Every refusal in _approve_unlocked, in source order, plus the new
# human-confirmed candidate binding.
APPROVE_REFUSALS = [
    ("not awaiting review", _set("builder", status="needs_human"), "expected 'awaiting_review'"),
    ("review incomplete", _set("builder", review_status="running"), "independent review is not complete"),
    ("verdict not pass", _set("builder", review_verdict="changes_required"), "review verdict is changes_required"),
    ("no review job", _drop("builder", "review_job_id"), "review job id is missing"),
    ("review job missing", _drop_reviewer, "job not found: rv1"),
    ("linked job not reviewer", _set("reviewer", role="builder"), "is not a reviewer"),
    ("reviewer for other builder", _set("reviewer", builder_job_id="x9"), "linked to another builder"),
    ("reviewer not complete", _set("reviewer", status="reviewing"), "is not complete"),
    ("reviewer verdict not pass", _set("reviewer", review_verdict="changes_required"), "verdict is not pass"),
    ("integration not passed", _set("builder", integration_status="failed"), "integration did not pass"),
    ("no integrated commit", _drop("builder", "integrated_candidate_commit"), "no integrated candidate commit"),
    ("builder reviewed other commit", _set("builder", reviewed_commit="d" * 40), "review did not target the exact"),
    ("reviewer candidate differs", _set("reviewer", candidate_commit="d" * 40), "candidate does not match integrated"),
    ("reviewer reviewed other commit", _set("reviewer", reviewed_commit="d" * 40), "reviewer did not target the exact"),
    ("main dirty", _git(main_dirty=True), "main worktree is not clean"),
    ("no integration base", _drop("builder", "integration_base_commit"), "integration base commit is missing"),
    ("stale main", _git(main_head="e" * 40), "stale main"),
    ("integration worktree path", _set("builder", integration_worktree="/opt/sids-ai-command-center"), "unexpected integration worktree"),
    ("integration worktree gone", _remove_integration, "integration worktree does not exist"),
    ("integration branch name", _set("builder", integration_branch="main"), "unexpected integration branch"),
    ("integrated HEAD moved", _git(integrated_head="d" * 40), "integrated worktree head changed"),
    ("integrated worktree dirty", _git(integrated_dirty=True), "integrated worktree changed after review"),
    ("integration branch moved", _git(branch_head="d" * 40), "integrated branch changed after review"),
]


@pytest.mark.parametrize("name,mutate,reason", APPROVE_REFUSALS,
                         ids=[case[0] for case in APPROVE_REFUSALS])
def test_approve_refusals_never_advance_main(
        job_review, fake_git, approvable, name, mutate, reason):
    mutate(job_review, fake_git, *approvable)
    refused(lambda: job_review.approve(JOB, expected_candidate=INTEGRATED), reason)
    assert fake_git.ran("merge") == []
    assert job_review.r.records[f"sid:jobs:{JOB}"]["status"] != "merged"
    assert LOCK not in job_review.r.values, "approval lock must be released"


@pytest.mark.parametrize("confirmed", ["d" * 40, INTEGRATED[:12], ""])
def test_approve_refuses_candidate_the_human_did_not_confirm(
        job_review, fake_git, approvable, confirmed):
    refused(lambda: job_review.approve(JOB, expected_candidate=confirmed),
            "is not the integrated candidate")
    assert fake_git.ran("merge") == []
    # Refused before any Git inspection of main or the worktree.
    assert fake_git.calls == []
    assert LOCK not in job_review.r.values


def test_approve_refuses_while_another_approval_holds_the_lock(
        job_review, fake_git, approvable):
    job_review.r.values[LOCK] = "other-owner"
    refused(lambda: job_review.approve(JOB, expected_candidate=INTEGRATED),
            "another approval")
    assert fake_git.calls == []
    assert job_review.r.values[LOCK] == "other-owner", "must not release another owner's lock"


def test_approve_failed_merge_leaves_job_unmerged(job_review, fake_git, approvable):
    fake_git.failing.add(("merge", "--ff-only"))
    refused(lambda: job_review.approve(JOB, expected_candidate=INTEGRATED), "failed")
    assert job_review.r.records[f"sid:jobs:{JOB}"]["status"] == "awaiting_review"
    assert LOCK not in job_review.r.values


def test_cli_approve_passes_confirmed_candidate(job_review, monkeypatch):
    seen = []
    monkeypatch.setattr(job_review, "approve", lambda job, expected_candidate=None: seen.append((job, expected_candidate)))
    monkeypatch.setattr("sys.argv", ["job-review.py", "approve", JOB, "--candidate", INTEGRATED])
    job_review.main()
    monkeypatch.setattr("sys.argv", ["job-review.py", "approve", JOB])
    job_review.main()
    assert seen == [(JOB, INTEGRATED), (JOB, None)]


@pytest.mark.parametrize("candidate", [INTEGRATED[:12], INTEGRATED.upper(), "g" * 40])
def test_cli_approve_requires_full_lowercase_sha(job_review, monkeypatch, candidate):
    monkeypatch.setattr(job_review, "approve", lambda *a, **k: pytest.fail("approved"))
    monkeypatch.setattr("sys.argv", ["job-review.py", "approve", JOB, "--candidate", candidate])
    refused(job_review.main, "full 40-character")
