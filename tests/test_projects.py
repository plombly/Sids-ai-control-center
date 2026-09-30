"""Separate projects: every path, gate, lock, queue and scope claim is per project."""

import json
from pathlib import Path

import pytest

from sid_testing import INTEGRATED, MemoryRedis, ROOT, load_module


class ProjectRedis(MemoryRedis):
    def smembers(self, key):
        return set(self.values.get(key, set()))

    def sadd(self, key, *members):
        self.values.setdefault(key, set()).update(members)


def register(r, project_id, tmp_path, **fields):
    root = tmp_path / project_id
    record = {"id": project_id, "name": project_id.title(), "importance": "medium",
              "root": str(root), "repo": str(root / "repo"), "worktrees": str(root / "worktrees"),
              "logs": str(root / "logs"), "default_branch": "main", "gate_command": "",
              "status": "active", **fields}
    r.records[f"sid:projects:{project_id}"] = record
    r.sadd("sid:projects", project_id)
    return record


@pytest.fixture
def projects():
    return load_module(ROOT / "services/sid_projects.py")


# --- registry resolution ------------------------------------------------------------

def test_sid_resolves_without_any_registry(projects, monkeypatch):
    monkeypatch.setenv("REPO_ROOT", "/srv/sid")
    p = projects.load(ProjectRedis(), "")
    assert (p.id, str(p.repo), p.is_sid) == ("sid", "/srv/sid", True)
    assert (p.merge_queue_key, p.approval_lock_key, p.main_head_key) == (
        "sid:merge-queue", "sid:approval-lock:main", "sid:main-head"), "SID keeps its key names"


def test_registry_cannot_move_sids_paths(projects, tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_ROOT", "/srv/sid")
    r = ProjectRedis()
    register(r, "sid", tmp_path, repo="/tmp/elsewhere", importance="high", name="SID")
    p = projects.load(r, "sid")
    assert str(p.repo) == "/srv/sid" and p.importance == "high" and p.name == "SID"


def test_other_projects_have_their_own_everything(projects, tmp_path):
    r = ProjectRedis()
    register(r, "web-shop", tmp_path, gate_command="npm test", importance="high")
    p = projects.load(r, "web-shop")
    assert p.repo == tmp_path / "web-shop" / "repo" and p.gate_command == "npm test"
    assert (p.merge_queue_key, p.approval_lock_key, p.main_head_key) == (
        "sid:merge-queue:web-shop", "sid:approval-lock:web-shop", "sid:main-head:web-shop")


def test_unknown_project_is_an_error_not_a_fallback(projects):
    with pytest.raises(LookupError):
        projects.load(ProjectRedis(), "nope")


def test_job_project_resolution(projects, tmp_path):
    r = ProjectRedis()
    r.records["sid:jobs:b1"] = {"id": "b1", "project_id": "web-shop"}
    assert projects.job_project_id(r, {"id": "x", "project_id": "api"}) == "api"
    assert projects.job_project_id(r, {"id": "rv1", "builder_job_id": "b1"}) == "web-shop"
    assert projects.job_project_id(r, {"id": "rp1", "target_builder_id": "b1"}) == "web-shop"
    assert projects.job_project_id(r, {"id": "b1"}) == "web-shop"
    assert projects.job_project_id(r, {"id": "legacy"}) == "sid"


def test_all_projects_sid_first_archived_excluded(projects, tmp_path):
    r = ProjectRedis()
    register(r, "zeta", tmp_path)
    register(r, "alpha", tmp_path)
    register(r, "old", tmp_path, status="archived")
    assert [p.id for p in projects.all_projects(r)] == ["sid", "alpha", "zeta"]


# --- worker ---------------------------------------------------------------------------

@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_ROOT", str(tmp_path / "sid-repo"))
    module = load_module(ROOT / "services/worker/worker.py")
    module.redis = ProjectRedis()
    return module


def test_worker_switches_every_path_per_job_and_back(worker, tmp_path, monkeypatch):
    register(worker.redis, "web-shop", tmp_path)
    project = worker.sid_projects.load(worker.redis, "web-shop")
    worker.use_project(project)
    assert worker.REPO_ROOT == project.repo and worker.WORKTREE_ROOT == project.worktrees
    assert worker.LOG_ROOT == project.logs
    calls = []
    monkeypatch.setattr(worker.subprocess, "run", lambda cmd, **kw: calls.append(kw["cwd"]))
    worker.run_git("status")
    assert calls == [project.repo], "run_git resolves the repository at call time"
    worker.use_project(None)
    assert worker.REPO_ROOT == tmp_path / "sid-repo"


@pytest.mark.parametrize("gate,ok,text", [
    ("echo gate-ran", True, "gate-ran"), ("echo broken; exit 3", False, "broken"), ("", True, "no gate command"),
])
def test_other_projects_use_their_own_gate(worker, tmp_path, gate, ok, text):
    register(worker.redis, "web-shop", tmp_path, gate_command=gate)
    worker.use_project(worker.sid_projects.load(worker.redis, "web-shop"))
    result_ok, output = worker.run_tests(tmp_path)
    assert result_ok is ok and text in output


def test_other_projects_shell_allowlist_is_their_gate(worker, tmp_path):
    register(worker.redis, "web-shop", tmp_path, gate_command="npm test")
    worker.use_project(worker.sid_projects.load(worker.redis, "web-shop"))
    allowed = worker.repair_allowed_bash()
    assert "Bash(npm test)" in allowed and not any("pytest" in a for a in allowed)
    worker.use_project(None)
    assert any("pytest" in a for a in worker.repair_allowed_bash())


def test_job_for_unknown_project_fails_instead_of_running(worker):
    worker.redis.records["sid:jobs:b9"] = {"id": "b9", "project_id": "ghost"}
    worker._process_job(json.dumps({"id": "b9", "role": "builder", "project_id": "ghost", "prompt": "x"}))
    record = worker.redis.records["sid:jobs:b9"]
    assert record["status"] == "failed" and "unknown project: ghost" in record["error"]


def test_worktree_branches_from_the_projects_default_branch(worker, tmp_path):
    register(worker.redis, "web-shop", tmp_path, default_branch="trunk")
    worker.use_project(worker.sid_projects.load(worker.redis, "web-shop"))
    calls = []
    worker.run_git = lambda *a, cwd=None, check=True: calls.append(a) or type("R", (), {"returncode": 1})()
    worker.create_worktree("b1")
    assert calls[-1][-1] == "trunk"


# --- orchestrator -----------------------------------------------------------------------

@pytest.fixture
def orch(monkeypatch):
    module = load_module(ROOT / "services/orchestrator/orchestrator.py")
    module.r = ProjectRedis()
    return module


def test_goal_is_planned_in_its_projects_repo_and_jobs_carry_it(orch, tmp_path, monkeypatch):
    register(orch.r, "web-shop", tmp_path)
    seen = {}

    def planner(goal, atomic=False, info=None, project=None):
        seen["project"] = project.id
        return {"jobs": [{"number": 1, "title": "t", "task": "do", "scope": ["src/a.js"], "depends_on": []}]}

    monkeypatch.setattr(orch, "run_planner", planner)
    orch.r.records["sid:goals:g1"] = {"id": "g1", "goal": "x", "status": "queued", "project_id": "web-shop"}
    orch.process_goal(json.dumps({"id": "g1", "goal": "x", "project_id": "web-shop"}))
    assert seen["project"] == "web-shop"
    [job] = [j for k, j in orch.r.records.items() if k.startswith("sid:jobs:")]
    assert job["project_id"] == "web-shop"
    assert json.loads(orch.r.values["sid:jobs"][0])["project_id"] == "web-shop"


def test_goal_for_unknown_project_is_not_planned(orch, monkeypatch):
    monkeypatch.setattr(orch, "run_planner", lambda *a, **k: pytest.fail("must not plan"))
    orch.r.records["sid:goals:g1"] = {"id": "g1", "goal": "x", "status": "queued", "project_id": "ghost"}
    with pytest.raises(LookupError):
        orch.process_goal(json.dumps({"id": "g1", "goal": "x", "project_id": "ghost"}))


def test_same_files_in_different_projects_do_not_conflict(orch):
    scope = json.dumps(["src/app.js"])
    orch.r.records["sid:jobs:a"] = {"id": "a", "role": "builder", "status": "running", "scope": scope,
                                    "project_id": "web-shop"}
    b = {"id": "b", "role": "builder", "status": "blocked", "scope": scope, "project_id": "api"}
    assert orch.scope_conflict(b) is None
    b["project_id"] = "web-shop"
    assert orch.scope_conflict(b)


def test_planner_prompt_is_project_specific(orch, tmp_path, monkeypatch):
    register(orch.r, "web-shop", tmp_path)
    monkeypatch.setattr(orch, "repository_manifest", lambda repo=None: f"manifest of {repo}")
    project = orch.sid_projects.load(orch.r, "web-shop")
    prompt = orch.planner_prompt("goal", project=project)
    assert str(project.repo) in prompt and '"Web-Shop" (web-shop)' in prompt
    assert "registerPanel" not in prompt, "SID's UI rule is SID-only"
    assert "registerPanel" in orch.planner_prompt("goal")


def test_stale_reintegration_uses_each_projects_main(orch, tmp_path, monkeypatch):
    register(orch.r, "web-shop", tmp_path)
    heads = {}
    monkeypatch.setattr(orch, "main_head", lambda repo=None: heads.setdefault(str(repo), "h" * 40))
    seen = []
    monkeypatch.setattr(orch, "refresh_project_queue", lambda project, head=None: seen.append(project.id))
    orch.refresh_queued_candidates()
    assert seen == ["sid", "web-shop"]


# --- job-review (host authority) ----------------------------------------------------------

def test_actions_run_inside_the_jobs_project_and_restore(job_review, tmp_path):
    r = ProjectRedis(job_review.r.records)
    job_review.r = r
    register(r, "web-shop", tmp_path)
    r.records["sid:jobs:w1"] = {"id": "w1", "project_id": "web-shop"}
    sid_repo = job_review.REPO_ROOT
    seen = []

    @job_review.in_job_project
    def action(job_id):
        seen.append((job_review.REPO_ROOT, job_review.current_project().approval_lock_key))

    action("w1")
    assert seen == [((tmp_path / "web-shop" / "repo").resolve(), "sid:approval-lock:web-shop")]
    assert job_review.REPO_ROOT == sid_repo, "restored after the action"


def test_queue_approval_goes_to_the_projects_queue(job_review, tmp_path):
    r = ProjectRedis(job_review.r.records)
    job_review.r = r
    register(r, "web-shop", tmp_path)
    r.records["sid:jobs:w1"] = {
        "id": "w1", "project_id": "web-shop", "status": "awaiting_review", "review_status": "complete",
        "review_verdict": "pass", "integration_status": "passed", "integrated_candidate_commit": INTEGRATED,
        "reviewed_commit": INTEGRATED, "source_candidate_commits": '["s1"]'}
    job_review.queue_approval("w1", INTEGRATED)
    assert r.values["sid:merge-queue:web-shop"] == ["w1"]
    assert "sid:merge-queue" not in r.values


def test_merge_queue_processes_every_project_on_its_own_main(job_review, fake_git, tmp_path):
    r = ProjectRedis(job_review.r.records)
    job_review.r = r
    register(r, "web-shop", tmp_path)
    repos = []
    original = fake_git.__call__

    def git(*args, cwd=None, check=True):
        if args == ("rev-parse", "HEAD"):
            repos.append(Path(cwd or job_review.REPO_ROOT))
        return original(*args, cwd=cwd, check=check)

    job_review.git = git
    assert job_review.process_merge_queue() == []
    assert repos == [job_review.REPO_ROOT, (tmp_path / "web-shop" / "repo").resolve()]
    assert set(k for k in r.values if k.startswith("sid:main-head")) == {"sid:main-head", "sid:main-head:web-shop"}


def test_project_gates_find_the_venvs_python_tooling(worker, tmp_path, monkeypatch):
    register(worker.redis, "py", tmp_path, gate_command='python3 -c "import pytest; print(\'pytest-ok\')"')
    worker.use_project(worker.sid_projects.load(worker.redis, "py"))
    monkeypatch.setattr(worker, "SID_PYTHON", "/opt/sid-venv/bin/python")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    ok, output = worker.run_tests(tmp_path)
    assert ok and "pytest-ok" in output
    assert worker.gate_env()["PATH"].startswith("/opt/sid-venv/bin" + __import__("os").pathsep)
