"""Fixtures for host-side tests; helpers live in laika_testing.py, which has
a unique module name (both tests/ and apps/api/tests have a conftest)."""

import pytest

from laika_testing import (  # noqa: F401  (re-exported for fixtures below)
    BASE, INTEGRATED, JOB, ROOT, FakeGit, MemoryRedis, load_module, make_builder,
)


@pytest.fixture
def memory_redis():
    return MemoryRedis()


@pytest.fixture
def job_review(memory_redis, tmp_path):
    """A fresh job-review module bound to fake Redis and a temp worktree root."""
    module = load_module(ROOT / "scripts/job-review.py")
    module.r = memory_redis
    module.REPO_ROOT = tmp_path / "main"
    module.REPO_ROOT.mkdir()
    module.WORKTREE_ROOT = tmp_path / "worktrees"
    module.WORKTREE_ROOT.mkdir()
    return module


@pytest.fixture
def fake_git(job_review):
    git = FakeGit(job_review)
    job_review.git = git
    return git


@pytest.fixture
def approvable(job_review, fake_git):
    """A builder/reviewer pair that satisfies every approval check."""
    integration = job_review.WORKTREE_ROOT / f"job-{JOB}-integration"
    integration.mkdir()
    record = make_builder(
        job_review,
        review_status="complete", review_verdict="pass", review_job_id="rv1",
        integration_status="passed", integration_base_commit=BASE,
        integrated_candidate_commit=INTEGRATED, reviewed_commit=INTEGRATED,
        integration_worktree=str(integration),
        integration_branch=f"laika/integration-{JOB}",
    )
    reviewer = {
        "id": "rv1", "role": "reviewer", "builder_job_id": JOB,
        "status": "review_complete", "review_verdict": "pass",
        "candidate_commit": INTEGRATED, "reviewed_commit": INTEGRATED,
    }
    job_review.r.records["laika:jobs:rv1"] = reviewer
    fake_git.main_head = BASE
    fake_git.integrated_head = INTEGRATED
    fake_git.branch_heads[f"laika/integration-{JOB}"] = INTEGRATED
    return record, reviewer, integration
