"""Scope-aware scheduling: jobs that change the same files never run at once."""

import json

import pytest

from laika_testing import MemoryRedis, ROOT, load_module


@pytest.fixture
def orch():
    module = load_module(ROOT / "services/orchestrator/orchestrator.py")
    module.r = MemoryRedis()
    return module


def put(orch, job_id, status, scope, created="1", deps=(), **fields):
    orch.r.records[f"laika:jobs:{job_id}"] = {
        "id": job_id, "role": "builder", "status": status, "prompt": f"do {job_id}",
        "scope": json.dumps(list(scope)), "dependencies": json.dumps(list(deps)),
        "created_at": created, **fields}
    return orch.r.records[f"laika:jobs:{job_id}"]


def job(orch, job_id):
    return orch.r.records[f"laika:jobs:{job_id}"]


def queued(orch):
    return [json.loads(raw)["id"] for raw in orch.r.values.get("laika:jobs", [])]


@pytest.mark.parametrize("a,b,overlap", [
    ("apps/web/app.js", "apps/web/app.js", True),
    ("apps/web", "apps/web/app.js", True),
    ("apps/web/", "apps/web/lib/api.js", True),
    ("apps/web/app.js", "apps/web/app.test.js", False),
    ("apps/api", "apps/api-v2/x.py", False),
    ("./apps/api/main.py", "apps/api/main.py", True),
])
def test_path_overlap(orch, a, b, overlap):
    x = put(orch, "x", "queued", [a])
    y = put(orch, "y", "running", [b])
    assert bool(orch.scope_conflict(x)) is overlap
    assert bool(orch.scope_conflict(y)) is overlap


@pytest.mark.parametrize("status,holds", [
    ("queued", True), ("claimed", True), ("running", True), ("testing", True),
    ("awaiting_review", True), ("needs_human", True),
    ("merged", False), ("rejected", False), ("completed_no_changes", False),
    ("blocked", False), ("blocked_failed_dependency", False),
])
def test_which_states_hold_files(orch, status, holds):
    put(orch, "a", status, ["apps/api/main.py"])
    b = put(orch, "b", "blocked", ["apps/api/main.py"])
    assert bool(orch.scope_conflict(b)) is holds


def test_failed_build_awaiting_retry_keeps_its_files(orch):
    put(orch, "a", "test_failed", ["apps/api/main.py"], build_attempt="1")
    assert orch.scope_conflict(put(orch, "b", "blocked", ["apps/api/main.py"]))
    job(orch, "a")["build_attempt"] = "2"  # exhausted: final, releases
    assert orch.scope_conflict(job(orch, "b")) is None


def test_no_scope_claims_nothing(orch):
    put(orch, "a", "running", [])
    assert orch.scope_conflict(put(orch, "b", "blocked", ["apps/api/main.py"])) is None
    put(orch, "c", "running", ["apps/api/main.py"])
    assert orch.scope_conflict(put(orch, "d", "blocked", [])) is None


def test_reason_names_files_and_holder(orch):
    put(orch, "a", "running", ["apps/web/app.js", "apps/web/index.html"])
    holder, reason = orch.scope_conflict(put(orch, "b", "blocked", ["apps/web/index.html"]))
    assert holder == "a"
    assert reason == "waiting for apps/web/index.html held by job a (running)"


def test_waiting_job_is_released_when_files_free_up(orch):
    put(orch, "a", "running", ["apps/api/main.py"])
    put(orch, "b", "blocked", ["apps/api/main.py"], created="2")
    orch.release_dependencies()
    assert job(orch, "b")["status"] == "blocked"
    assert "held by job a" in job(orch, "b")["blocked_reason"]
    assert queued(orch) == []
    job(orch, "a")["status"] = "merged"
    orch.release_dependencies()
    assert job(orch, "b")["status"] == "queued"
    assert job(orch, "b")["blocked_reason"] == ""
    assert queued(orch) == ["b"]


def test_release_is_fifo_and_one_at_a_time_per_file(orch):
    put(orch, "late", "blocked", ["apps/api/main.py"], created="3")
    put(orch, "early", "blocked", ["apps/api/main.py"], created="2")
    put(orch, "other", "blocked", ["apps/web/app.js"], created="4")
    orch.release_dependencies()
    assert sorted(queued(orch)) == ["early", "other"], "disjoint work runs in parallel"
    assert job(orch, "late")["status"] == "blocked"
    assert "held by job early" in job(orch, "late")["blocked_reason"]


def test_dependencies_still_come_first(orch):
    put(orch, "dep", "running", ["apps/other.py"])
    put(orch, "b", "blocked", ["apps/api/main.py"], created="2", deps=["dep"])
    orch.release_dependencies()
    assert job(orch, "b")["status"] == "blocked"
    job(orch, "dep")["status"] = "merged"
    orch.release_dependencies()
    assert queued(orch) == ["b"]


def test_planned_jobs_wait_for_busy_files_and_are_retry_eligible(orch, monkeypatch):
    put(orch, "busy", "awaiting_review", ["apps/web/app.js"])
    orch.r.records["laika:goals:g1"] = {"id": "g1", "goal": "ui work", "status": "queued"}
    monkeypatch.setattr(orch, "run_planner", lambda goal, atomic=False, info=None, project=None: {"jobs": [
        {"number": 1, "title": "ui", "task": "t1", "scope": ["apps/web/app.js"], "depends_on": []},
        {"number": 2, "title": "api", "task": "t2", "scope": ["apps/api/main.py"], "depends_on": []},
    ]})
    monkeypatch.setattr(orch, "repository_manifest", lambda repo=None: "")
    orch.process_goal(json.dumps({"id": "g1", "goal": "ui work"}))
    jobs = {j["title"]: j for k, j in orch.r.records.items() if k.startswith("laika:jobs:") and j.get("title")}
    assert jobs["ui"]["status"] == "blocked"
    assert "held by job busy" in jobs["ui"]["blocked_reason"]
    assert jobs["api"]["status"] == "queued"
    assert queued(orch) == [jobs["api"]["id"]]
    assert jobs["ui"]["build_attempt"] == jobs["api"]["build_attempt"] == "1"


def test_planner_is_told_which_files_are_busy(orch, monkeypatch):
    monkeypatch.setattr(orch, "repository_manifest", lambda repo=None: "")
    put(orch, "busy", "running", ["apps/api/main.py"])
    put(orch, "done", "merged", ["apps/web/app.js"])
    prompt = orch.planner_prompt("goal")
    assert "  - apps/api/main.py" in prompt
    assert "apps/web/app.js\n" not in prompt.split("in-flight jobs")[1]
    assert "registerPanel" in prompt


def test_a_job_too_big_to_review_is_split_by_asking_the_planner_again(orch, monkeypatch):
    orch.r.records["laika:goals:g2"] = {"id": "g2", "goal": "a multiplayer game", "status": "queued"}
    big = {"jobs": [{"number": 1, "title": "whole server", "task": "t", "size": "L", "depends_on": [],
                     "scope": [f"server/f{i}.js" for i in range(9)] + ["tests/test_server.js"]}]}
    split = {"jobs": [{"number": 1, "title": "game rules", "task": "t", "size": "M", "scope": ["server/game.js"], "depends_on": []},
                      {"number": 2, "title": "rooms", "task": "t", "size": "M", "scope": ["server/rooms.js"], "depends_on": [1]}]}
    asked = []
    monkeypatch.setattr(orch, "run_planner", lambda goal, atomic=False, info=None, project=None: asked.append(goal) or (big if len(asked) == 1 else split))
    monkeypatch.setattr(orch, "repository_manifest", lambda repo=None: "")
    orch.process_goal(json.dumps({"id": "g2", "goal": "a multiplayer game"}))
    assert len(asked) == 2 and "PLANNER FEEDBACK" in asked[1] and '"whole server" (sized L)' in asked[1]
    titles = sorted(j["title"] for k, j in orch.r.records.items() if k.startswith("laika:jobs:") and j.get("title"))
    assert titles == ["game rules", "rooms"]
    assert orch.oversized_jobs(big, atomic=True) == []                      # atomic goals are one job by request
    assert orch.oversized_jobs({"jobs": [{"number": 1, "size": "M", "scope": ["a.js", "b.js", "test/a.test.js"]}]}) == []
    assert "There is no L" in orch.planner_prompt("goal") and "S|M|L" not in orch.planner_prompt("goal")


def test_a_split_plan_over_the_job_limit_falls_back_to_the_first_plan(orch, monkeypatch):
    # goal 72caf977b7a4: a valid 9-job plan with one big job; the split came back with 11 jobs
    # and the whole goal failed with "planner produced too many jobs".
    job = lambda n, size="M", files=1: {"number": n, "title": f"job {n}", "task": "t", "size": size,
                                        "depends_on": [], "scope": [f"src/f{n}_{i}.py" for i in range(files)]}
    first = {"jobs": [job(n) for n in range(1, 9)] + [job(9, files=10)]}
    split = {"jobs": [job(n) for n in range(1, 12)]}
    orch.r.records["laika:goals:g9"] = {"id": "g9", "goal": "nginx manager", "status": "queued"}
    asked = []
    monkeypatch.setattr(orch, "run_planner", lambda goal, atomic=False, info=None, project=None: asked.append(goal) or (first if len(asked) == 1 else split))
    monkeypatch.setattr(orch, "repository_manifest", lambda repo=None: "")
    orch.process_goal(json.dumps({"id": "g9", "goal": "nginx manager"}))
    assert len(asked) == 2 and "at most 10 jobs" in asked[1]
    goal = orch.r.records["laika:goals:g9"]
    assert goal["status"] != "planning_failed" and "split not used" in goal["replanned"]
    jobs = [j for k, j in orch.r.records.items() if k.startswith("laika:jobs:") and j.get("goal_id") == "g9"]
    assert len(jobs) == 9


def test_problem_goals_are_diagnosed_first_with_recent_failures(orch, monkeypatch):
    monkeypatch.setattr(orch, "repository_manifest", lambda repo=None: "")
    assert orch.reports_problem("The lobby crashes when a second player joins")
    assert orch.reports_problem("Why doesn't the score update?")
    assert not orch.reports_problem("Add a dark mode toggle to the settings page")
    assert orch.plan_timeout("fix the login error") == orch.DIAGNOSE_TIMEOUT
    assert orch.plan_timeout("add a pause menu") == orch.PLAN_TIMEOUT
    orch.r.records["laika:jobs:f1"] = {"id": "f1", "project_id": "laika", "status": "test_failed", "title": "Scores",
                                       "error": "AssertionError: expected 3 got 2\nmore", "updated_at": str(__import__("time").time())}
    orch.r.records["laika:app-status:laika"] = {"state": "crashed", "error": "exit 1", "log": "TypeError: x is undefined"}
    prompt = orch.planner_prompt("the scoreboard is broken")
    assert "DIAGNOSE BEFORE YOU PLAN" in prompt and "file:line" in prompt
    assert 'Job "Scores" test_failed: AssertionError: expected 3 got 2' in prompt
    assert "App: crashed: exit 1" in prompt and "TypeError: x is undefined" in prompt
    plain = orch.planner_prompt("add a pause menu")
    assert "DIAGNOSE" not in plain and "Recent problems" not in plain and "Do not broadly inspect" in plain


def test_planning_interrupted_by_a_restart_is_queued_again(orch):
    orch.r.records["laika:goals:g7"] = {"id": "g7", "goal": "fix the lobby", "status": "planning", "project_id": "laika",
                                        "atomic": "false"}
    orch.r.values["laika:goals:g7:planning"] = orch.ORCHESTRATOR_ID   # the claim of the process that died
    orch.recover_interrupted_planning()
    assert orch.r.records["laika:goals:g7"]["status"] == "planning"   # a live claim is left alone
    orch.recover_interrupted_planning(startup=True)                   # ...but at startup it is ours and dead
    goal = orch.r.records["laika:goals:g7"]
    assert goal["status"] == "queued" and goal["plan_restarts"] == "1"
    queued = json.loads(orch.r.values[orch.GOAL_QUEUE][-1])
    assert queued == {"id": "g7", "goal": "fix the lobby", "atomic": False, "project_id": "laika"}
    goal["status"] = "planning"
    orch.recover_interrupted_planning()                               # claim gone (expired): queued again
    goal["status"] = "planning"
    orch.recover_interrupted_planning()
    assert goal["status"] == "planning_failed" and "interrupted" in goal["error"]


def test_a_repair_gets_every_blocking_finding_as_a_checklist(orch):
    findings = ("## spec review\n- MET: rooms\n- BLOCKING: path traversal in server/index.js:8 (fileFor)\n"
                "- NOTE: naming\n- NOT MET (BLOCKING): the joined message lacks the slot\n- NON-BLOCKING: style")
    checklist = orch.blocking_checklist(findings)
    assert checklist.splitlines() == ["1. [ ] BLOCKING: path traversal in server/index.js:8 (fileFor)",
                                      "2. [ ] NOT MET (BLOCKING): the joined message lacks the slot"]
    assert "every BLOCKING item" in orch.blocking_checklist("all fine")


class ScriptingRedis(MemoryRedis):
    """MemoryRedis plus the scripted, pipelined light read job_rows() uses."""

    def __init__(self):
        super().__init__()
        self.light_reads = 0

    def register_script(self, source):
        store = self

        class Script:
            def __call__(self, keys, args, client):
                client.ops.append((keys[0], json.loads(args[0])))
        return Script()

    def pipeline(self, transaction=False):
        store = self

        class Pipe:
            ops = []

            def execute(self, raise_on_error=True):
                store.light_reads += 1
                out = []
                for key, limits in self.ops:
                    flat = []
                    for field, value in (store.records.get(key) or {}).items():
                        limit = limits.get(field, -1)
                        if limit < 0:
                            flat += [field, value]
                        elif limit > 0:
                            flat += [field, value[:limit]]
                    out.append(flat)
                return out
        pipe = Pipe()
        pipe.ops = []
        return pipe


def test_one_light_read_per_pass_and_candidates_read_in_full(orch):
    orch.r = ScriptingRedis()
    put(orch, "a", "awaiting_review", ["x.py"], review_status="complete", review_verdict="changes_required",
        review_findings="BLOCKING: " + "y" * 5000)
    [(key, light)] = orch.job_rows()
    assert "prompt" not in light and "review_findings" not in light and light["status"] == "awaiting_review"
    orch.begin_pass()
    orch.job_rows(), orch.job_rows(), orch.job_rows()
    assert orch.r.light_reads == 2, "one shared read inside a pass (plus the one before it)"
    orch.job_rows(fresh=True)
    assert orch.r.light_reads == 3, "fresh always reads"
    orch.end_pass()
    orch.job_rows()
    assert orch.r.light_reads == 4, "outside a pass nothing is reused"


def test_release_inside_a_pass_still_holds_files_one_at_a_time(orch):
    orch.r = ScriptingRedis()
    put(orch, "late", "blocked", ["apps/api/main.py"], created="3")
    put(orch, "early", "blocked", ["apps/api/main.py"], created="2")
    orch.begin_pass()
    orch.release_dependencies()   # the pass read still says "blocked" for early when late is checked
    orch.end_pass()
    assert queued(orch) == ["early"] and job(orch, "late")["status"] == "blocked"


def test_two_jobs_of_one_plan_on_the_same_file_do_not_start_together(orch, monkeypatch):
    orch.r.records["laika:goals:g5"] = {"id": "g5", "goal": "shop", "status": "queued"}
    plan = {"jobs": [{"number": 1, "title": "cart", "task": "t", "size": "M", "scope": ["shop.py"], "depends_on": []},
                     {"number": 2, "title": "checkout", "task": "t", "size": "M", "scope": ["shop.py"], "depends_on": []}]}
    monkeypatch.setattr(orch, "run_planner", lambda goal, atomic=False, info=None, project=None: plan)
    monkeypatch.setattr(orch, "repository_manifest", lambda repo=None: "")
    orch.process_goal(json.dumps({"id": "g5", "goal": "shop"}))
    jobs = {j["title"]: j for k, j in orch.r.records.items() if k.startswith("laika:jobs:") and j.get("goal_id") == "g5"}
    assert jobs["cart"]["status"] == "queued" and jobs["checkout"]["status"] == "blocked"
    assert "held by job" in jobs["checkout"]["blocked_reason"]
    goal = orch.r.records["laika:goals:g5"]
    assert goal["status"] == "running" and sorted(json.loads(goal["jobs"])) == sorted(j["id"] for j in jobs.values())


def test_acceptance_criteria_and_misspelled_fields(orch):
    plan = {"jobs": [{"number": 1, "title": "a", "task": "t", "scope": ["a.py"], "acceptance_criteria": "it works"},
                     {"number": 2, "title": "b", "task": "t", "scope": ["b.py"], "dependencies": [1],
                      "acceptance": ["  x  ", ""], "notes": "free text"}]}
    jobs = orch.validate_plan(plan)
    assert jobs[0]["acceptance"] == ["it works"] and jobs[1]["depends_on"] == [1] and jobs[1]["acceptance"] == ["x"]
    assert "notes" not in jobs[1]
    assert "job 2: read 'dependencies' as 'depends_on'" in plan["warnings"]
    assert "job 2: ignored unknown field 'notes'" in plan["warnings"]
    prompt = orch.scoped_builder_prompt({"task": "t", "scope": [], "acceptance": ["GET /x works"]})
    assert "Done when (the independent reviewer checks each one):\n1. GET /x works" in prompt


def test_reviewer_lines_become_a_checklist():
    worker = load_module(ROOT / "services/worker/worker.py", "worker_criteria_test")
    messages = ["Findings...\nCRITERION 1: MET - app.py adds /ping\n**CRITERION 2**: NOT MET - no test\nVERDICT: CHANGES_REQUIRED"]
    review = worker.parse_criteria_review(messages, ["/ping exists", "a test covers it", "docs updated"])
    assert [(r["met"], r["note"]) for r in review] == [(True, "app.py adds /ping"), (False, "no test"), (None, "")]
    assert "CRITERION <number>: MET" in worker.criteria_review_note(["x"]) and worker.criteria_review_note([]) == ""


def test_no_goal_is_planned_while_all_new_work_is_paused(orch):
    orch.r.values["laika:pause"] = '{"by": "admin"}'
    orch.r.values[orch.GOAL_QUEUE] = ['{"id": "g1", "goal": "x"}']
    submitted = []
    executor = type("E", (), {"submit": lambda self, fn, raw: submitted.append(raw)})()
    orch.submit_queued_goals(executor, set())
    assert submitted == [] and orch.r.values[orch.GOAL_QUEUE] == ['{"id": "g1", "goal": "x"}']
