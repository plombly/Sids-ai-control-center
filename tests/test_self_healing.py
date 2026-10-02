"""Self-healing: the pipeline carries work past worker loss, failed builds,
failed reviews and failed repairs without a human, within fixed bounds."""

import json
from pathlib import Path

import pytest

from laika_testing import GitResult, MemoryRedis, ROOT, load_module

T0 = 1_800_000_000.0
QUEUE = "laika:jobs"


@pytest.fixture
def orch():
    module = load_module(ROOT / "services/orchestrator/orchestrator.py")
    module.r = MemoryRedis()
    module._lost_suspects.clear()
    return module


def put(orch, job_id, **fields):
    orch.r.records[f"laika:jobs:{job_id}"] = {"id": job_id, **fields}
    return orch.r.records[f"laika:jobs:{job_id}"]


def job(orch, job_id):
    return orch.r.records[f"laika:jobs:{job_id}"]


def worker(orch, name, holds=""):
    orch.r.records[f"laika:workers:{name}"] = {"id": name, "status": "idle", "job_id": holds}


def queued_ids(orch):
    return [json.loads(raw)["id"] for raw in orch.r.values.get(QUEUE, [])]


# --- failed builds are rebuilt once, then handed to a human ----------------------

def failed_builder(orch, status="failed", **fields):
    return put(orch, "b1", **{
        "role": "builder", "status": status, "build_attempt": "1",
        "goal_id": "g1", "prompt": "Add a thing", "candidate_commit": "c" * 40,
        "source_candidate_commits": '["cc"]', "review_verdict": "changes_required",
        "dispatch_reserved_at": "1", "error": "Codex exceeded 600 second builder timeout",
        "repair_attempts": "2", "review_findings_history": '[{"f": 1}]', **fields})


def test_failed_build_is_rebuilt_from_main_with_the_failure_reason(orch):
    failed_builder(orch)
    orch.retry_failed_builds()
    b = job(orch, "b1")
    assert b["status"] == "queued"
    assert b["build_attempt"] == "2"
    assert b["base_prompt"] == "Add a thing"
    assert b["prompt"].startswith("Add a thing\n\nAttempt 1 of this job failed")
    assert "600 second builder timeout" in b["prompt"]
    for gone in ("candidate_commit", "source_candidate_commits", "review_verdict", "error"):
        assert gone not in b, gone
    assert b["repair_attempts"] == "0"
    assert b["review_findings_history"] == '[{"f": 1}]', "findings history is kept"
    assert queued_ids(orch) == ["b1"]
    payload = json.loads(orch.r.values[QUEUE][0])
    assert payload["prompt"] == b["prompt"] and payload["role"] == "builder"


def test_retry_is_dispatched_once(orch):
    failed_builder(orch)
    orch.retry_failed_builds()
    job(orch, "b1")["status"] = "failed"  # fails again before the next pass
    job(orch, "b1")["build_attempt"] = "1"  # (simulate a stale re-read)
    orch.retry_failed_builds()
    assert queued_ids(orch) == ["b1"]


def test_second_retry_prompt_does_not_compound(orch):
    failed_builder(orch)
    orch.retry_failed_builds()
    b = job(orch, "b1")
    b.update(status="failed", error="second failure", max_build_attempts="3")
    orch.retry_failed_builds()
    assert b["prompt"].count("failed and was discarded") == 1
    assert "second failure" in b["prompt"]


def test_test_failure_reason_includes_gate_output(orch, tmp_path):
    log = tmp_path / "b1-tests.log"
    log.write_text("x" * 5000 + "FAILED test_thing - assert 1 == 2")
    failed_builder(orch, "test_failed", test_log=str(log))
    orch.retry_failed_builds()
    assert "FAILED test_thing" in job(orch, "b1")["prompt"]
    assert "LAIka test gate failed" in job(orch, "b1")["retry_reason"]


def test_integration_conflict_is_rebuilt_on_current_main(orch):
    failed_builder(orch, "integration_failed", integration_error="cannot apply source candidate")
    orch.retry_failed_builds()
    assert job(orch, "b1")["status"] == "queued"
    assert "cannot apply source candidate" in job(orch, "b1")["prompt"]


def test_exhausted_build_goes_to_needs_human(orch):
    failed_builder(orch, "test_failed", build_attempt="2")
    orch.retry_failed_builds()
    b = job(orch, "b1")
    assert b["status"] == "needs_human"
    assert b["needs_human_kind"] == "build"
    assert b["failed_status"] == "test_failed"
    assert "after 2 attempt(s)" in b["needs_human_reason"]
    assert queued_ids(orch) == []


def test_extended_build_limit_is_honored(orch):
    failed_builder(orch, build_attempt="2", max_build_attempts="4")
    orch.retry_failed_builds()
    assert job(orch, "b1")["build_attempt"] == "3"


def test_legacy_failures_are_never_retried(orch):
    legacy = failed_builder(orch)
    legacy.pop("build_attempt")
    before = dict(legacy)
    orch.retry_failed_builds()
    assert job(orch, "b1") == before
    assert queued_ids(orch) == []


@pytest.mark.parametrize("role", ["reviewer", "repair", "integrate"])
def test_only_builders_are_rebuilt(orch, role):
    put(orch, "x1", role=role, status="failed", build_attempt="1")
    orch.retry_failed_builds()
    assert job(orch, "x1")["status"] == "failed"


def test_new_builders_are_retry_eligible(orch):
    orch.create_job("g1", {"title": "t", "task": "do it"}, {})
    [record] = [v for k, v in orch.r.records.items() if k.startswith("laika:jobs:")]
    assert record["build_attempt"] == "1"


# --- a pending retry is not a terminal failure ------------------------------------

def test_dependents_wait_while_a_failed_dependency_will_be_retried(orch):
    failed_builder(orch)
    put(orch, "d1", role="builder", status="blocked", dependencies='["b1"]', prompt="p")
    orch.release_dependencies()
    assert job(orch, "d1")["status"] == "blocked"


def test_dependents_wait_on_needs_human(orch):
    failed_builder(orch, build_attempt="2")
    orch.retry_failed_builds()
    put(orch, "d1", role="builder", status="blocked", dependencies='["b1"]', prompt="p")
    orch.release_dependencies()
    assert job(orch, "d1")["status"] == "blocked"


def test_terminal_failure_still_cascades(orch):
    put(orch, "b1", role="builder", status="rejected")
    put(orch, "d1", role="builder", status="blocked", dependencies='["b1"]', prompt="p")
    orch.release_dependencies()
    assert job(orch, "d1")["status"] == "blocked_failed_dependency"


def test_goal_stays_open_while_retry_is_pending(orch):
    failed_builder(orch)
    orch.r.records["laika:goals:g1"] = {"id": "g1", "status": "running", "jobs": '["b1"]'}
    orch.update_goals()
    assert orch.r.records["laika:goals:g1"]["status"] == "running"
    job(orch, "b1").pop("build_attempt")  # legacy: terminal
    orch.update_goals()
    assert orch.r.records["laika:goals:g1"]["status"] == "failed"


# --- jobs whose worker died are declared lost ---------------------------------------

@pytest.mark.parametrize("status", ["claimed", "running", "testing", "reviewing",
                                    "repairing", "integrating", "queued"])
def test_job_nobody_holds_is_failed_after_confirmation(orch, status):
    worker(orch, "w1", holds="other")
    put(orch, "j1", role="reviewer", status=status, worker_id="w1")
    orch.recover_lost_jobs(T0)
    assert job(orch, "j1")["status"] == status, "first sighting only suspects"
    orch.recover_lost_jobs(T0 + orch.LOST_CONFIRM_SECONDS - 1)
    assert job(orch, "j1")["status"] == status
    orch.recover_lost_jobs(T0 + orch.LOST_CONFIRM_SECONDS)
    assert job(orch, "j1")["status"] == "failed"
    assert job(orch, "j1")["error"].startswith("worker lost")


def test_held_and_queued_jobs_are_not_lost(orch):
    worker(orch, "w1", holds="j1")
    put(orch, "j1", status="running", worker_id="w1")
    put(orch, "j2", status="queued")
    orch.r.rpush(QUEUE, json.dumps({"id": "j2"}))
    for t in (T0, T0 + 1000):
        orch.recover_lost_jobs(t)
    assert job(orch, "j1")["status"] == "running"
    assert job(orch, "j2")["status"] == "queued"


def test_suspicion_resets_when_job_is_seen_alive(orch):
    worker(orch, "w1")
    put(orch, "j1", status="running", worker_id="w1")
    orch.recover_lost_jobs(T0)
    worker(orch, "w1", holds="j1")
    orch.recover_lost_jobs(T0 + 30)
    worker(orch, "w1")
    orch.recover_lost_jobs(T0 + 70)  # absent again, but only just
    assert job(orch, "j1")["status"] == "running"


def test_no_recovery_while_an_old_worker_cannot_report_its_job(orch):
    orch.r.records["laika:workers:w1"] = {"id": "w1", "status": "working"}  # no job_id field
    put(orch, "j1", status="running", worker_id="w1")
    put(orch, "b1", role="builder", status="awaiting_review", review_status="failed")
    for t in (T0, T0 + 1000):
        orch.recover_lost_jobs(t)
        orch.recover_stalled_reviews(t)
    assert job(orch, "j1")["status"] == "running"
    assert "last_integrate_job_id" not in job(orch, "b1")


def test_terminal_jobs_are_ignored(orch):
    for status in ("merged", "review_complete", "failed", "rejected", "blocked",
                   "needs_human", "awaiting_review", "repair_complete"):
        put(orch, f"j{status}", status=status)
    orch.recover_lost_jobs(T0)
    orch.recover_lost_jobs(T0 + 1000)
    assert all(job(orch, f"j{s}")["status"] == s for s in (
        "merged", "review_complete", "failed", "rejected", "blocked",
        "needs_human", "awaiting_review", "repair_complete"))


# --- reviews that cannot finish are re-integrated and re-reviewed -------------------

def stalled_builder(orch, **fields):
    return put(orch, "b1", **{
        "role": "builder", "status": "awaiting_review", "goal_id": "g1",
        "review_status": "failed", "review_job_id": "rv1",
        "review_error": "Reviewer Codex exited with status 1",
        "integration_status": "passed", "integrated_candidate_commit": "c" * 40,
        "source_candidate_commits": '["s1"]', **fields})


def run_stall_recovery(orch):
    orch.recover_stalled_reviews(T0)
    orch.recover_stalled_reviews(T0 + orch.LOST_CONFIRM_SECONDS)


def test_failed_review_is_reintegrated_and_rereviewed(orch):
    worker(orch, "w1")
    stalled_builder(orch)
    orch.r.values["laika:integration-lock:b1"] = "w1:dead"
    run_stall_recovery(orch)
    b = job(orch, "b1")
    integrate_id = b["last_integrate_job_id"]
    assert job(orch, integrate_id)["role"] == "integrate"
    assert job(orch, integrate_id)["target_builder_id"] == "b1"
    assert queued_ids(orch) == [integrate_id]
    for gone in orch.DERIVED_REVIEW_FIELDS:
        assert gone not in b, gone
    assert b["source_candidate_commits"] == '["s1"]'
    assert b["review_recoveries"] == "1"
    assert "exited with status 1" in b["review_recovery_reason"]
    assert "laika:integration-lock:b1" not in orch.r.values, "stale lock released"


def test_review_recovery_waits_for_confirmation(orch):
    worker(orch, "w1")
    stalled_builder(orch)
    orch.recover_stalled_reviews(T0)
    assert queued_ids(orch) == []


@pytest.mark.parametrize("related", ["b1", "rv1", "rp1", "rp0", "ig1"])
def test_builder_with_live_related_work_is_not_stalled(orch, related):
    worker(orch, "w1", holds=related)
    stalled_builder(orch, repair_job_id="rp1", last_repair_job_id="rp0",
                    last_integrate_job_id="ig1")
    run_stall_recovery(orch)
    assert job(orch, "b1").get("review_recoveries") is None


def test_builder_with_queued_related_work_is_not_stalled(orch):
    worker(orch, "w1")
    stalled_builder(orch, review_status="queued")
    orch.r.rpush(QUEUE, json.dumps({"id": "rv1"}))
    run_stall_recovery(orch)
    assert job(orch, "b1").get("review_recoveries") is None


def test_completed_review_is_a_resting_state(orch):
    worker(orch, "w1")
    stalled_builder(orch, review_status="complete")
    run_stall_recovery(orch)
    assert queued_ids(orch) == []


def test_review_recoveries_are_bounded(orch):
    worker(orch, "w1")
    stalled_builder(orch, review_recoveries=str(2))
    run_stall_recovery(orch)
    b = job(orch, "b1")
    assert b["status"] == "needs_human"
    assert b["needs_human_kind"] == "review"
    assert queued_ids(orch) == []


# --- end to end: a worker dies mid-build ---------------------------------------------

def test_worker_death_mid_build_leads_to_a_rebuild(orch):
    worker(orch, "w1")  # restarted: idle, holds nothing
    put(orch, "b1", role="builder", status="running", worker_id="w1",
        build_attempt="1", prompt="Add a thing", goal_id="g1")
    put(orch, "d1", role="builder", status="blocked", dependencies='["b1"]', prompt="p")
    for t in (T0, T0 + orch.LOST_CONFIRM_SECONDS):
        orch.recover_lost_jobs(t)
        orch.retry_failed_builds()
        orch.release_dependencies()
    assert job(orch, "b1")["status"] == "queued"
    assert "worker lost" in job(orch, "b1")["prompt"]
    assert queued_ids(orch) == ["b1"]
    assert job(orch, "d1")["status"] == "blocked"


# --- worker side ------------------------------------------------------------------------

class WorkerGit:
    def __init__(self):
        self.calls = []
        self.head = "c" * 40
        self.dirty = False
        self.ancestor = True
        self.branches = set()

    def __call__(self, *args, cwd=None, check=True):
        self.calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return GitResult(self.head + "\n")
        if args == ("status", "--porcelain"):
            return GitResult(" M x\n" if self.dirty else "")
        if args[:2] == ("merge-base", "--is-ancestor"):
            return GitResult(returncode=0 if self.ancestor else 1)
        if args[:2] == ("reset", "--hard"):
            self.head, self.dirty = args[2], False
            return GitResult()
        if args[0] == "show-ref":
            return GitResult(returncode=0 if args[-1].removeprefix("refs/heads/") in self.branches else 1)
        if args[0] in {"clean", "worktree", "branch"}:
            return GitResult()
        raise AssertionError(f"unexpected git call {args}")


@pytest.fixture
def worker_module(tmp_path):
    module = load_module(ROOT / "services/worker/worker.py")
    module.redis = MemoryRedis()
    module.WORKTREE_ROOT = tmp_path
    module.git_fake = WorkerGit()
    module.run_git = module.git_fake
    return module


def test_heartbeat_names_the_held_job_and_clears_it(worker_module):
    beats = []
    worker_module._process_job = lambda raw: beats.append(
        dict(worker_module.redis.records[worker_module.worker_key()]))
    worker_module.process_job(json.dumps({"id": "j42", "role": "reviewer"}))
    assert beats[0]["job_id"] == "j42"
    after = worker_module.redis.records[worker_module.worker_key()]
    worker_module.heartbeat()
    assert worker_module.redis.records[worker_module.worker_key()]["job_id"] == ""
    assert after["status"] in {"working", "idle"}


def test_create_worktree_replaces_its_own_stale_worktree(worker_module, tmp_path):
    stale = tmp_path / "job-b1"
    stale.mkdir()
    worker_module.git_fake.branches.add("laika/job-b1")
    original = worker_module.git_fake.__call__

    def git(*args, cwd=None, check=True):
        if args[:3] == ("worktree", "remove", "--force"):
            stale.rmdir()
        return original(*args, cwd=cwd, check=check)

    worker_module.run_git = git
    worker_module.create_worktree("b1")
    calls = worker_module.git_fake.calls
    assert ("worktree", "remove", "--force", str(stale)) in calls
    assert ("branch", "-D", "laika/job-b1") in calls
    assert calls[-1] == ("worktree", "add", "-b", "laika/job-b1", str(stale), "main")


def test_create_worktree_refuses_if_stale_worktree_cannot_be_removed(worker_module, tmp_path):
    (tmp_path / "job-b1").mkdir()
    with pytest.raises(RuntimeError, match="could not be removed"):
        worker_module.create_worktree("b1")
    assert not any(c[:2] == ("worktree", "add") for c in worker_module.git_fake.calls)


CANDIDATE = "c" * 40


def test_restore_discards_leftover_edits(worker_module, tmp_path):
    git = worker_module.git_fake
    git.dirty = True
    worker_module.restore_candidate(tmp_path, CANDIDATE)
    assert ("reset", "--hard", CANDIDATE) in git.calls
    assert ("clean", "-fd") in git.calls


def test_restore_rewinds_commits_made_after_the_candidate(worker_module, tmp_path):
    git = worker_module.git_fake
    git.head = "d" * 40
    worker_module.restore_candidate(tmp_path, CANDIDATE)
    assert git.head == CANDIDATE


def test_restore_refuses_unrelated_history(worker_module, tmp_path):
    git = worker_module.git_fake
    git.head, git.ancestor = "d" * 40, False
    with pytest.raises(RuntimeError, match="does not match reviewed candidate"):
        worker_module.restore_candidate(tmp_path, CANDIDATE)
    assert not any(c[0] == "reset" for c in git.calls)


def repair_setup(worker_module, tmp_path):
    (tmp_path / "job-b1").mkdir()
    worker_module.redis.records["laika:jobs:b1"] = {
        "id": "b1", "status": "awaiting_review", "review_verdict": "changes_required",
        "worktree": str(tmp_path / "job-b1"), "candidate_commit": CANDIDATE,
    }
    job = {"id": "rp1", "target_builder_id": "b1", "prompt": "fix"}
    return job, "laika:jobs:rp1", tmp_path / "rp1.jsonl", tmp_path / "rp1-tests.log"


def test_repair_starts_from_clean_candidate_after_an_earlier_failure(worker_module, tmp_path):
    job, key, log, test_log = repair_setup(worker_module, tmp_path)
    worker_module.git_fake.dirty = True  # left by a failed repair
    worker_module.run_codex = lambda *a: (_ for _ in ()).throw(RuntimeError("stop here"))
    with pytest.raises(RuntimeError, match="stop here"):
        worker_module.process_repair_job(job, key, log, test_log)
    resets = [c for c in worker_module.git_fake.calls if c[0] == "reset"]
    assert len(resets) == 2, "restored at start and again after the failure"
    assert worker_module.redis.records[key]["status"] == "repairing"


def test_repair_timeout_restores_candidate_and_propagates(worker_module, tmp_path):
    job, key, log, test_log = repair_setup(worker_module, tmp_path)

    def codex(*args):
        worker_module.git_fake.dirty = True
        raise RuntimeError("Codex exceeded 360 second repair timeout")

    worker_module.run_codex = codex
    with pytest.raises(RuntimeError, match="timeout"):
        worker_module.process_repair_job(job, key, log, test_log)
    assert worker_module.git_fake.dirty is False


def test_repair_gate_failure_restores_candidate(worker_module, tmp_path):
    job, key, log, test_log = repair_setup(worker_module, tmp_path)

    def codex(*args):
        worker_module.git_fake.dirty = True
        return 0, 1.0

    worker_module.run_codex = codex
    worker_module.parse_codex_log = lambda path: ("s", {k: 0 for k in (
        "total_tokens", "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_tokens", "uncached_input_tokens", "effective_tokens", "command_count")})
    worker_module.run_tests = lambda worktree, network=False: (False, "FAILED")
    worker_module.process_repair_job(job, key, log, test_log)
    assert worker_module.redis.records[key]["status"] == "test_failed"
    assert worker_module.redis.records["laika:jobs:b1"]["repair_status"] == "test_failed"
    assert worker_module.git_fake.dirty is False


def test_builder_claim_records_first_attempt_only_if_unset():
    source = (ROOT / "services/worker/worker.py").read_text()
    assert 'redis.hsetnx(key, "build_attempt", "1")' in source


# --- a retry is told the actual failure, not the warnings -------------------------------

PYTEST_OUTPUT = """$ python -m pytest apps/api/tests -q
..F.
=================================== FAILURES ===================================
_____________________________ test_thing _____________________________
>       fake.hashes["laika:jobs:review-1"].update({})
E       KeyError: 'laika:jobs:review-1'
=============================== warnings summary ===============================
""" + "DeprecationWarning: on_event is deprecated\n" * 200 + """
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED apps/api/tests/test_dashboard_api.py::test_thing
1 failed, 3 passed in 1.0s
"""


def test_retry_reason_shows_the_failure_not_the_warnings(orch):
    excerpt = orch.test_output_excerpt(PYTEST_OUTPUT)
    assert "KeyError: 'laika:jobs:review-1'" in excerpt
    assert "FAILED apps/api/tests/test_dashboard_api.py::test_thing" in excerpt
    assert "DeprecationWarning" not in excerpt
    assert len(excerpt) <= 3000


def test_excerpt_without_pytest_sections_drops_warnings(orch):
    output = "$ compileall\nSyntaxError: bad thing\n=== warnings summary ===\nnoise\n-- Docs: x\ntrailer\n"
    excerpt = orch.test_output_excerpt(output)
    assert "SyntaxError: bad thing" in excerpt
    assert "noise" not in excerpt


def test_integration_gate_failure_names_the_failing_check(orch):
    failed_builder(orch, "integration_failed",
                   integration_error="deterministic integration gate failed",
                   integration_result=json.dumps({"returncode": 1, "stdout":
                       "[ PASS ] api-tests\n[ FAIL ] web-tests\nAssertionError: expected 10"}))
    orch.retry_failed_builds()
    prompt = job(orch, "b1")["prompt"]
    assert "[ FAIL ] web-tests" in prompt and "AssertionError: expected 10" in prompt


def test_next_repair_learns_why_the_last_one_was_discarded(orch, tmp_path):
    log = tmp_path / "r1-tests.log"
    log.write_text("FAILED test/settings_screen_test.dart: expected 'LAIka Home'\n")
    put(orch, "r1", role="repair", status="test_failed", test_log=str(log))
    put(orch, "b1", role="builder", last_repair_job_id="r1")
    note = orch.previous_repair_note(job(orch, "b1"))
    assert "previous repair attempt was discarded" in note and "expected 'LAIka Home'" in note
    job(orch, "r1")["status"] = "repair_complete"
    assert orch.previous_repair_note(job(orch, "b1")) == ""
    assert orch.previous_repair_note({"id": "b2"}) == ""
