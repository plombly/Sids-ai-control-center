"""Behavior of scripts/job-review.py, the host-side operator authority."""

import pytest

JOB = "b1"


def builder(job_review, status="awaiting_review", **fields):
    worktree = job_review.WORKTREE_ROOT / f"job-{JOB}"
    worktree.mkdir(exist_ok=True)
    record = {
        "id": JOB, "role": "builder", "status": status,
        "worktree": str(worktree), "branch": f"sid/job-{JOB}",
        **fields,
    }
    job_review.r.records[f"sid:jobs:{JOB}"] = record
    return record


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
