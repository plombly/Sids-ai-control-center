"""Internet access for project tests on request (services/network_access.py),
end to end through the worker, orchestrator, job-review and operator."""

import json
import subprocess
import sys

import pytest

from laika_testing import JOB, ROOT, MemoryRedis, load_module, make_builder

sys.path.insert(0, str(ROOT / "services"))
import network_access  # noqa: E402
import project_sandbox  # noqa: E402

NPM_OFFLINE = "npm ERR! request to https://registry.npmjs.org/x failed, reason: getaddrinfo EAI_AGAIN registry.npmjs.org"


@pytest.mark.parametrize("line", [
    NPM_OFFLINE, "socket.gaierror: [Errno -3] Temporary failure in name resolution",
    "curl: (6) Could not resolve host: api.stripe.com", "dial tcp: lookup api.github.com: no such host",
    "requests.exceptions.ConnectionError: Failed to establish a new connection",
])
def test_network_failures_are_recognised(line):
    assert network_access.evidence(f"ok\n{line}\nmore") == [line[:240]]


def test_ordinary_failures_and_decided_jobs_make_no_request():
    assert network_access.request({}, "AssertionError: 1 != 2", "tests") is None
    assert network_access.request({"network_denied": "1"}, NPM_OFFLINE, "tests") is None
    assert network_access.request({"network_allowed": "1"}, NPM_OFFLINE, "tests") is None
    assert network_access.request({}, NPM_OFFLINE, "tests", project_fields={"gate_network": "always"}) is None


def test_requests_carry_evidence_or_the_builders_reason_without_credentials():
    fields = network_access.request({}, "fatal: unable to access 'https://bob:hunter2@github.com/x.git/'", "tests")
    assert fields["network_request"] == "pending" and "hunter2" not in fields["network_request_reason"]
    assert "//***@github.com" in fields["network_request_reason"]
    fields = network_access.request({}, "1 failed", "tests", agent_text="Done.\nNEEDS_NETWORK: tests call the Stripe sandbox")
    assert fields["network_request_reason"].startswith("The builder says: tests call the Stripe sandbox")


def test_gate_sandbox_gets_network_only_when_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv("LAIKA_SANDBOX_GATE_NETWORK", raising=False)
    project = type("P", (), {"id": "shop", "is_builtin": False, "repo": tmp_path / "repo", "root": tmp_path})()
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr(project_sandbox, "enabled", lambda: True)
    assert "--unshare-net" in project_sandbox.command(["true"], project, tmp_path / "repo", kind="gate")
    assert "--unshare-net" not in project_sandbox.command(["true"], project, tmp_path / "repo", kind="gate", network=True)


# --- worker -----------------------------------------------------------------------

@pytest.fixture
def worker(tmp_path):
    module = load_module(ROOT / "services/worker/worker.py")
    module.redis = MemoryRedis()
    module.redis.records["laika:projects:shop"] = {"id": "shop", "repo": str(tmp_path), "worktrees": str(tmp_path),
                                                 "logs": str(tmp_path), "root": str(tmp_path)}
    module.PROJECT = module.laika_projects.load(module.redis, "shop")
    module.redis.records["laika:jobs:b1"] = {"id": "b1", "status": "testing"}
    return module


def test_worker_records_a_request_and_passes_network_to_the_gate(worker):
    assert worker.gate_network("b1") is False
    worker.note_network_need("b1", NPM_OFFLINE, "tests")
    record = worker.redis.records["laika:jobs:b1"]
    assert record["network_request"] == "pending" and record["network_request_step"] == "tests"
    record["network_allowed"] = "1"
    assert worker.gate_network("b1") is True
    worker.redis.records["laika:jobs:b2"] = {"id": "b2"}
    worker.redis.records["laika:projects:shop"]["gate_network"] = "always"
    assert worker.gate_network("b2") is True


def test_laika_itself_never_files_requests(worker):
    worker.PROJECT = worker.laika_projects.load(worker.redis, "laika")
    worker.note_network_need("b1", NPM_OFFLINE, "tests")
    assert "network_request" not in worker.redis.records["laika:jobs:b1"]
    assert "NEEDS_NETWORK" not in worker.efficiency_prefix("builder")


def test_project_builders_are_told_how_to_ask(worker):
    assert "NEEDS_NETWORK:" in worker.efficiency_prefix("builder")
    assert "NEEDS_NETWORK:" not in worker.efficiency_prefix("reviewer")


# --- orchestrator -------------------------------------------------------------------

@pytest.fixture
def orch():
    module = load_module(ROOT / "services/orchestrator/orchestrator.py")
    module.r = MemoryRedis()
    return module


def test_failed_build_waiting_for_internet_goes_to_the_operator(orch):
    orch.r.records["laika:jobs:b1"] = {"id": "b1", "role": "builder", "status": "test_failed", "build_attempt": "1",
                                     "prompt": "x", "network_request": "pending", "network_request_step": "tests",
                                     "network_request_reason": NPM_OFFLINE}
    orch.retry_failed_builds()
    record = orch.r.records["laika:jobs:b1"]
    assert (record["status"], record["needs_human_kind"], record["network_resume"]) == ("needs_human", "network", "build")
    assert record["failed_status"] == "test_failed" and "EAI_AGAIN" in record["needs_human_reason"]
    assert not orch.r.values.get("laika:jobs")  # no blind rebuild


def test_repair_waiting_for_internet_goes_to_the_operator(orch):
    orch.r.records["laika:jobs:b1"] = {"id": "b1", "role": "builder", "status": "awaiting_review", "review_status": "complete",
                                     "review_verdict": "changes_required", "repair_attempts": "1",
                                     "network_request": "pending", "review_job_id": "r1"}
    orch.queue_repairs()
    record = orch.r.records["laika:jobs:b1"]
    assert (record["status"], record["needs_human_kind"], record["network_resume"]) == ("needs_human", "network", "repair")


def test_denied_rebuild_is_told_to_work_offline(orch):
    orch.r.records["laika:jobs:b1"] = {"id": "b1", "role": "builder", "status": "test_failed", "build_attempt": "1",
                                     "max_build_attempts": "2", "prompt": "Add x", "network_denied": "1",
                                     "network_request": "answered:deny"}
    orch.retry_failed_builds()
    assert network_access.DENIED_NOTE in orch.r.records["laika:jobs:b1"]["prompt"]


# --- job-review / operator ---------------------------------------------------------------

@pytest.fixture
def waiting(job_review, tmp_path):
    job_review.r.records["laika:projects:shop"] = {"id": "shop", "repo": str(tmp_path / "main"),
                                                 "worktrees": str(tmp_path / "worktrees"), "logs": str(tmp_path)}

    def make(resume, **fields):
        return make_builder(job_review, "needs_human", project_id="shop", needs_human_kind="network",
                            network_resume=resume, network_request="pending", network_request_reason="EAI_AGAIN",
                            build_attempt="2", failed_status="integration_failed", repair_attempts="2", **fields)
    return make


def test_allow_once_resumes_the_rebuild_with_internet(job_review, waiting):
    waiting("build")
    job_review.network(JOB, "once")
    record = job_review.r.records[f"laika:jobs:{JOB}"]
    assert record["status"] == "integration_failed" and record["max_build_attempts"] == "3"
    assert record["network_allowed"] == "1" and record["needs_human_kind"] == ""
    assert record["network_request"] == "answered:once"
    event = json.loads(job_review.r.values["laika:events:shop"][0])
    assert event["kind"] == "network" and "allowed internet" in event["title"]


def test_always_sets_the_project_and_resumes_a_repair(job_review, waiting):
    waiting("repair", max_repair_attempts="2")
    job_review.network(JOB, "always")
    record = job_review.r.records[f"laika:jobs:{JOB}"]
    assert record["status"] == "awaiting_review" and record["max_repair_attempts"] == "3"
    assert job_review.r.records["laika:projects:shop"]["gate_network"] == "always"


def test_deny_keeps_tests_offline(job_review, waiting):
    waiting("build")
    job_review.network(JOB, "deny")
    record = job_review.r.records[f"laika:jobs:{JOB}"]
    assert record["network_denied"] == "1" and "network_allowed" not in record


def test_network_answers_are_refused_for_other_jobs(job_review, waiting):
    make_builder(job_review, "needs_human", project_id="shop", needs_human_kind="build")
    with pytest.raises(SystemExit):
        job_review.network(JOB, "once")
    waiting("build")
    with pytest.raises(SystemExit):
        job_review.network(JOB, "sometimes")
    with pytest.raises(SystemExit) as caught:
        job_review.extend(JOB, 1)
    assert "internet-access decision" in caught.value.message


def test_cli_and_operator_know_the_network_actions(job_review, waiting, monkeypatch):
    waiting("build")
    monkeypatch.setattr("sys.argv", ["job-review.py", "network", JOB, "once"])
    job_review.main()
    assert job_review.r.records[f"laika:jobs:{JOB}"]["network_allowed"] == "1"
    operator = load_module(ROOT / "services/operator/laika_operator.py")
    assert {"network_once", "network_always", "network_deny"} <= set(operator.DEFAULT_ALLOWED_ACTIONS.split(","))
    assert all(a in operator.ACTIONS for a in operator.NETWORK_ACTIONS)
