"""Project groups: a parent controls its children; a parent's goal plans
work across the group; agents read the other members read-only."""

import json
import subprocess
import sys

import pytest

from sid_testing import ROOT, MemoryRedis, load_module

sys.path.insert(0, str(ROOT / "services"))
import project_reference  # noqa: E402
import sid_projects  # noqa: E402


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(path, files):
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", "init", cwd=path)


@pytest.fixture
def group(tmp_path):
    r = MemoryRedis()
    for pid, extra in (("shop", {"importance": "high", "gate_network": "always"}),
                       ("shop-app", {"parent": "shop", "importance": "low"}),
                       ("shop-admin", {"parent": "shop"}), ("other", {})):
        root = tmp_path / pid
        make_repo(root / "repo", {"README.md": f"# {pid}\n", ".env.example": "X=1\n"})
        r.records[f"sid:projects:{pid}"] = {"id": pid, "name": pid.title(), "status": "active", "root": str(root),
                                            "repo": str(root / "repo"), "worktrees": str(root / "worktrees"),
                                            "logs": str(root / "logs"), **extra}
        r.sadd("sid:projects", pid)
    return r, tmp_path


def test_children_follow_their_parent(group):
    r, _ = group
    app = sid_projects.load(r, "shop-app")
    assert app.parent == "shop" and app.importance == "high"  # its own "low" is overridden
    assert sid_projects.effective_fields(r, "shop-app")["gate_network"] == "always"
    r.records["sid:projects:shop"]["status"] = "archived"
    assert sid_projects.load(r, "shop-app").status == "archived"
    assert sid_projects.children(r, "shop") == ["shop-admin", "shop-app"]


def test_a_parent_goal_spans_its_active_children_only(group):
    r, _ = group
    assert [m.id for m in sid_projects.group(r, sid_projects.load(r, "shop"))] == ["shop", "shop-admin", "shop-app"]
    assert [m.id for m in sid_projects.group(r, sid_projects.load(r, "shop-app"))] == ["shop-app"]
    r.records["sid:projects:shop-admin"]["status"] = "archived"
    assert [m.id for m in sid_projects.group(r, sid_projects.load(r, "shop"))] == ["shop", "shop-app"]


@pytest.mark.parametrize("child,parent,problem", [
    ("other", "shop", ""), ("sid", "shop", "SID"), ("other", "sid", "SID"), ("other", "other", "own parent"),
    ("other", "nope", "unknown"), ("other", "shop-app", "one level"), ("shop", "other", "one level"),
])
def test_group_rules(group, child, parent, problem):
    r, _ = group
    found = sid_projects.check_parent(r, child, parent)
    assert (problem.lower() in found.lower()) if problem else found == ""


def test_reference_copies_are_tracked_files_at_main_and_refresh(group):
    r, tmp_path = group
    app, shop = sid_projects.load(r, "shop-app"), sid_projects.load(r, "shop")
    (shop.repo / "secret.env").write_text("TOKEN=1\n")  # untracked: never copied
    copy = project_reference.refresh(app, shop)
    assert copy == app.root / "reference" / "shop"
    assert (copy / "README.md").read_text() == "# shop\n"
    assert not (copy / "secret.env").exists() and not (copy / ".git").exists()
    (shop.repo / "README.md").write_text("# shop v2\n")
    git("commit", "-qam", "v2", cwd=shop.repo)
    project_reference.refresh(app, shop)
    assert (copy / "README.md").read_text() == "# shop v2\n"
    copies = project_reference.prepare(app, sid_projects.group(r, shop))
    assert [m.id for m, _ in copies] == ["shop", "shop-admin"]
    note = project_reference.note(app, "Shop", copies)
    assert "Read-only copies" in note and str(copy) in note


def test_worker_gives_agents_the_group(group):
    r, _ = group
    worker = load_module(ROOT / "services/worker/worker.py")
    worker.redis = r
    worker.use_project(sid_projects.load(r, "shop-app"))
    worker.prepare_group_context()
    assert "part of the project group" in worker.efficiency_prefix("builder")
    assert len(worker.agent_cli.EXTRA_DIRS) == 2
    assert worker.agent_cli.claude_command("reviewer", "m", 1)[2] == "--add-dir"
    worker.use_project(sid_projects.load(r, "other"))
    worker.prepare_group_context()
    assert worker.GROUP_NOTE == "" and worker.agent_cli.EXTRA_DIRS == []
    assert "--add-dir" not in worker.agent_cli.claude_command("reviewer", "m", 1)


# --- planning across the group ------------------------------------------------------------

@pytest.fixture
def orch(group):
    r, _ = group
    module = load_module(ROOT / "services/orchestrator/orchestrator.py")
    module.r = r
    return module


def test_parent_planner_sees_every_member_and_names_the_project(orch):
    prompt = orch.planner_prompt("Add a loyalty program", project=sid_projects.load(orch.r, "shop"))
    assert "PROJECT GROUP" in prompt
    for member in ("shop", "shop-app", "shop-admin"):
        assert f"id: {member};" in prompt
    assert '"project": "member id"' in prompt and prompt.count("README.md") >= 3
    single = orch.planner_prompt("x", project=sid_projects.load(orch.r, "shop-app"))
    assert "PROJECT GROUP" not in single and '"project"' not in single


def test_group_plans_are_validated(orch):
    plan = {"jobs": [{"number": 1, "title": "t", "task": "x", "project": "elsewhere"}]}
    with pytest.raises(ValueError, match="not in this group"):
        orch.validate_plan(plan, members=["shop", "shop-app"])
    plan = {"jobs": [{"number": 1, "title": "t", "task": "x"}]}
    assert orch.validate_plan(plan, members=["shop", "shop-app"])[0]["project"] == "shop"


def test_a_parent_goal_creates_jobs_in_members_with_cross_project_dependencies(orch, monkeypatch):
    plan = {"summary": "s", "jobs": [
        {"number": 1, "project": "shop", "title": "API", "task": "Add /points", "scope": ["README.md"], "depends_on": []},
        {"number": 2, "project": "shop-app", "title": "Screen", "task": "Show points", "scope": ["README.md"], "depends_on": [1]},
    ]}
    monkeypatch.setattr(orch, "run_planner", lambda *a, **k: plan)
    orch.r.records["sid:goals:g1"] = {"id": "g1", "project_id": "shop"}
    orch.process_goal(json.dumps({"id": "g1", "goal": "loyalty", "project_id": "shop"}))
    jobs = {j["title"]: j for j in (orch.r.records[f"sid:jobs:{i}"] for i in json.loads(orch.r.records["sid:goals:g1"]["jobs"]))}
    assert jobs["API"]["project_id"] == "shop" and jobs["API"]["status"] == "queued"
    assert jobs["Screen"]["project_id"] == "shop-app" and jobs["Screen"]["status"] == "blocked"
    assert json.loads(jobs["Screen"]["dependencies"]) == [jobs["API"]["id"]]
    # Same file name in two repositories: no scope conflict between them.
    assert orch.scope_conflict({**jobs["Screen"], "status": "queued"}) is None


# --- delete / restore ------------------------------------------------------------------------

def test_a_parent_with_children_cannot_be_deleted(group, monkeypatch):
    r, _ = group
    sid_project = load_module(ROOT / "scripts/sid-project.py")
    monkeypatch.setattr(sid_project, "get_redis", lambda: r)
    with pytest.raises(sid_project.ProjectError, match="child projects"):
        sid_project.delete(type("A", (), {"id": "shop", "confirm": "shop"})())


# --- goal assistant ----------------------------------------------------------------------------

def test_assistant_on_a_parent_reads_the_group(group):
    r, _ = group
    assist = load_module(ROOT / "scripts/sid-assist.py")
    note = assist.group_note(r, sid_projects.load(r, "shop"))
    assert "which member" in note and len(assist.agent_cli.EXTRA_DIRS) == 2
    assert assist.group_note(r, sid_projects.load(r, "other")) == "" and assist.agent_cli.EXTRA_DIRS == []
