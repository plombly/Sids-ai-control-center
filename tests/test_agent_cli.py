"""Claude provider: role routing, sandbox flags, fallback, parsing."""

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

from sid_testing import MemoryRedis, ROOT, load_module

FAKE_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys, time
    prompt = sys.stdin.read()
    with open(os.environ["FAKE_CLAUDE_ARGS"], "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}) + "\\n")
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    base = {"type": "result", "session_id": "sess-1", "num_turns": 3,
            "total_cost_usd": 0.0421, "api_error_status": None,
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 200,
                      "cache_read_input_tokens": 3000, "output_tokens": 50,
                      "output_tokens_details": {"thinking_tokens": 7}}}
    if mode == "sleep":
        time.sleep(30)
    if mode == "crash":
        print("Traceback: boom", file=sys.stderr)
        sys.exit(3)
    if mode == "limit":
        print(json.dumps({**base, "is_error": True, "subtype": "success",
                          "result": "You've hit your session limit · resets 4:50am"}))
        sys.exit(1)
    if mode == "budget":
        print(json.dumps({**base, "is_error": True, "subtype": "error_max_budget_usd",
                          "result": "Budget exceeded"}))
        sys.exit(1)
    if mode == "noise":
        print("warning: something on stderr", file=sys.stderr)
    result = os.environ.get("FAKE_CLAUDE_RESULT", "done")
    for aspect, marker in (("SPEC", "YOUR FOCUS: specification"),
                           ("SAFETY", "YOUR FOCUS: security"), ("TESTS", "YOUR FOCUS: tests")):
        if marker in prompt:
            result = os.environ.get(f"FAKE_ASPECT_{aspect}", result)
    if mode == "aspect_limit" and "YOUR FOCUS: security" in prompt:
        print(json.dumps({**base, "is_error": True, "subtype": "success",
                          "result": "You've hit your session limit"}))
        sys.exit(1)
    print(json.dumps({**base, "is_error": False, "subtype": "success", "result": result}))
''')


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(calls))
    monkeypatch.delenv("ROLE_PROVIDERS", raising=False)

    def recorded():
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text().splitlines()]

    return recorded


@pytest.fixture
def agent_cli():
    module = load_module(ROOT / "services/agent_cli.py")
    module.POLL_SECONDS = 0.05
    return module


def flag(argv, name):
    return argv[argv.index(name) + 1]


# --- configuration ------------------------------------------------------------

def test_default_roles_put_claude_on_planning_review_and_repair(agent_cli):
    assert {r: agent_cli.role_provider(r) for r in agent_cli.ROLES} == {
        "planner": "claude", "builder": "codex", "reviewer": "claude", "repair": "claude"}


def test_role_providers_env_overrides_defaults(agent_cli, monkeypatch):
    monkeypatch.setenv("ROLE_PROVIDERS", "reviewer=codex, builder = claude")
    assert agent_cli.role_provider("reviewer") == "codex"
    assert agent_cli.role_provider("builder") == "claude"
    assert agent_cli.role_provider("repair") == "claude"


def test_unknown_provider_is_a_configuration_error(agent_cli, monkeypatch):
    monkeypatch.setenv("ROLE_PROVIDERS", "reviewer=gemini")
    with pytest.raises(ValueError, match="unknown provider"):
        agent_cli.role_provider("reviewer")


def test_lighter_models_by_default_and_overridable(agent_cli, monkeypatch):
    assert agent_cli.claude_model("reviewer") == "sonnet"
    assert agent_cli.claude_model("repair") == "sonnet"
    assert agent_cli.claude_model("planner") == "opus"
    monkeypatch.setenv("CLAUDE_REVIEWER_MODEL", "claude-haiku-4-5-20251001")
    assert agent_cli.claude_model("reviewer") == "claude-haiku-4-5-20251001"
    monkeypatch.setenv("CLAUDE_REPAIR_BUDGET_USD", "0.5")
    assert agent_cli.claude_budget("repair") == 0.5


# --- sandbox flags --------------------------------------------------------------

@pytest.mark.parametrize("role", ["planner", "reviewer"])
def test_planner_and_reviewer_are_read_only(agent_cli, role):
    argv = agent_cli.claude_command(role, "sonnet", 1.0)
    assert flag(argv, "--tools") == "Read,Grep,Glob"
    assert "--permission-mode" not in argv
    assert "--allowedTools" not in argv


@pytest.mark.parametrize("role", ["repair", "builder"])
def test_repair_edits_only_in_worktree_with_allowlisted_shell(agent_cli, role):
    argv = agent_cli.claude_command(role, "sonnet", 2.0, ("Bash(git diff *)",))
    assert flag(argv, "--tools") == "Read,Edit,Write,Grep,Glob,Bash"
    assert flag(argv, "--permission-mode") == "acceptEdits"
    assert argv[-2:] == ["--allowedTools", "Bash(git diff *)"], "variadic flag must be last"
    assert "--dangerously-skip-permissions" not in argv
    assert "bypassPermissions" not in argv


def test_every_call_is_headless_bounded_and_isolated(agent_cli):
    argv = agent_cli.claude_command("reviewer", "sonnet", 1.5)
    for required in ("-p", "--no-session-persistence", "--strict-mcp-config"):
        assert required in argv
    assert flag(argv, "--output-format") == "json"
    assert flag(argv, "--max-budget-usd") == "1.50"
    assert "Work only in the current working directory" in flag(argv, "--append-system-prompt")


# --- running ----------------------------------------------------------------------

def test_run_claude_success(agent_cli, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "VERDICT: PASS")
    ticks = []
    run = agent_cli.run_claude("reviewer", "review this", tmp_path, tmp_path / "log.json", 30,
                               tick=lambda: ticks.append(1))
    assert run.ok and not run.unavailable
    assert run.text == "VERDICT: PASS"
    [call] = fake_claude()
    assert call["prompt"] == "review this", "prompt goes in on stdin"
    assert "review this" not in call["argv"]
    assert call["cwd"] == str(tmp_path)
    assert flag(call["argv"], "--model") == "sonnet"


def test_run_claude_tolerates_stderr_noise(agent_cli, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "noise")
    assert agent_cli.run_claude("reviewer", "x", tmp_path, tmp_path / "l.json", 30).ok


def test_plan_limit_is_unavailable(agent_cli, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    run = agent_cli.run_claude("reviewer", "x", tmp_path, tmp_path / "l.json", 30)
    assert not run.ok and run.unavailable
    assert "session limit" in run.describe_error()


def test_budget_exhaustion_is_a_failure_not_unavailability(agent_cli, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "budget")
    run = agent_cli.run_claude("repair", "x", tmp_path, tmp_path / "l.json", 30)
    assert not run.ok and not run.unavailable
    assert "error_max_budget_usd" in run.describe_error()


def test_crash_without_result(agent_cli, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    run = agent_cli.run_claude("repair", "x", tmp_path, tmp_path / "l.json", 30)
    assert run.result is None and not run.ok and not run.unavailable
    assert "status 3" in run.describe_error()


def test_timeout_kills_the_process(agent_cli, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    run = agent_cli.run_claude("repair", "x", tmp_path, tmp_path / "l.json", 0.5)
    assert run.timed_out and not run.ok
    assert run.duration < 15


def test_usage_maps_to_codex_keys(agent_cli):
    result = {"is_error": False, "total_cost_usd": 0.0421, "usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 3000, "output_tokens": 50,
        "output_tokens_details": {"thinking_tokens": 7}}}
    usage = agent_cli.claude_usage(result)
    assert usage["input_tokens"] == "3210"
    assert usage["cached_input_tokens"] == "3000"
    assert usage["uncached_input_tokens"] == "210"
    assert usage["effective_tokens"] == "260"
    assert usage["total_tokens"] == "3260"
    assert usage["reasoning_tokens"] == "7"
    assert usage["cost_usd"] == "0.0421"


def test_agent_messages_reads_both_formats(agent_cli, tmp_path):
    codex = tmp_path / "codex.jsonl"
    codex.write_text("\n".join(json.dumps(e) for e in [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
        {"type": "item.completed", "item": {"type": "command_execution"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "VERDICT: PASS"}},
    ]))
    assert agent_cli.agent_messages(codex) == ["first", "VERDICT: PASS"]
    claude = tmp_path / "claude.json"
    claude.write_text("stderr line\n" + json.dumps({"type": "result", "result": "VERDICT: CHANGES_REQUIRED"}))
    assert agent_cli.agent_messages(claude) == ["VERDICT: CHANGES_REQUIRED"]
    assert agent_cli.read_claude_result(codex) is None


def test_cooldown(agent_cli):
    fake = MemoryRedis()
    assert not agent_cli.claude_cooling_down(fake)
    agent_cli.start_cooldown(fake, "limit")
    assert agent_cli.claude_cooling_down(fake)


# --- worker: provider per role, fallback, telemetry ---------------------------------

@pytest.fixture
def worker(tmp_path, fake_claude, monkeypatch):
    monkeypatch.setenv("REVIEW_ASPECTS", "spec")  # single review unless a test opts in
    module = load_module(ROOT / "services/worker/worker.py")
    module.redis = MemoryRedis()
    module.agent_cli.POLL_SECONDS = 0.05
    module.codex_calls = []

    def fake_codex(job, worktree, log_path):
        module.codex_calls.append(job["id"])
        Path(log_path).write_text(json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "codex says hi"}}))
        return 0, 1.0

    module.run_codex = fake_codex
    return module


def job(role, **fields):
    return {"id": f"{role}1", "role": role, "prompt": "do the thing", **fields}


def test_reviewer_runs_on_claude_and_records_provider_model_cost(worker, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "Looks good.\nVERDICT: PASS")
    log = tmp_path / "rv.json"
    assert worker.run_agent(job("reviewer"), tmp_path, log)[0] == 0
    record = worker.redis.records["sid:jobs:reviewer1"]
    assert (record["provider"], record["model"], record["cost_usd"]) == ("claude", "sonnet", "0.0421")
    assert worker.codex_calls == []
    assert worker.parse_review_verdict(worker.agent_cli.agent_messages(log))[0] == "pass"
    session, usage = worker.parse_codex_log(log)
    assert session == "sess-1" and usage["effective_tokens"] == "260"


def test_builder_stays_on_codex(worker, tmp_path, fake_claude):
    worker.run_agent(job("builder", model="gpt-5.6-luna"), tmp_path, tmp_path / "b.jsonl")
    assert worker.codex_calls == ["builder1"]
    assert fake_claude() == []
    assert worker.redis.records["sid:jobs:builder1"]["provider"] == "codex"


def test_repair_gets_the_allowlisted_shell(worker, tmp_path, fake_claude):
    worker.run_agent(job("repair"), tmp_path, tmp_path / "r.json")
    argv = fake_claude()[0]["argv"]
    allowed = argv[argv.index("--allowedTools") + 1:]
    assert f"Bash({worker.SID_PYTHON} -m pytest *)" in allowed
    assert all(a.startswith("Bash(") for a in allowed)


def test_plan_limit_falls_back_to_codex_and_cools_down(worker, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    worker.run_agent(job("reviewer"), tmp_path, tmp_path / "rv.json")
    record = worker.redis.records["sid:jobs:reviewer1"]
    assert worker.codex_calls == ["reviewer1"]
    assert record["provider"] == "codex"
    assert "session limit" in record["provider_fallback"]
    assert worker.agent_cli.claude_cooling_down(worker.redis)
    # The next Claude-role job skips Claude entirely while cooling down.
    worker.run_agent(job("repair"), tmp_path, tmp_path / "r.json")
    assert len(fake_claude()) == 1
    assert worker.codex_calls == ["reviewer1", "repair1"]
    assert "cooling down" in worker.redis.records["sid:jobs:repair1"]["provider_fallback"]


@pytest.mark.parametrize("mode,match", [("budget", "error_max_budget_usd"), ("crash", "status 3")])
def test_other_claude_failures_fail_the_job_without_fallback(worker, tmp_path, monkeypatch, mode, match):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    with pytest.raises(RuntimeError, match=match):
        worker.run_agent(job("repair"), tmp_path, tmp_path / "r.json")
    assert worker.codex_calls == []
    assert not worker.agent_cli.claude_cooling_down(worker.redis)


def test_claude_timeout_fails_the_job(worker, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    with pytest.raises(RuntimeError, match="Claude exceeded 1 second repair timeout"):
        worker.run_agent(job("repair", timeout_seconds="1"), tmp_path, tmp_path / "r.json")


# --- orchestrator: planner and repair findings ---------------------------------------------

@pytest.fixture
def orch(tmp_path, fake_claude):
    module = load_module(ROOT / "services/orchestrator/orchestrator.py")
    module.r = MemoryRedis()
    module.agent_cli.POLL_SECONDS = 0.05
    module.PLANNER_LOG_ROOT = tmp_path / "planner"
    module.REPO_ROOT = tmp_path
    module.repository_manifest = lambda repo=None: "apps/api/main.py"
    return module


def test_planner_uses_claude_opus_read_only(orch, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", 'Here:\n```json\n{"jobs": [{"title": "t"}]}\n```')
    assert orch.run_planner("add a thing") == {"jobs": [{"title": "t"}]}
    argv = fake_claude()[0]["argv"]
    assert flag(argv, "--model") == "opus"
    assert flag(argv, "--tools") == "Read,Grep,Glob"


def test_planner_falls_back_to_codex_when_claude_is_limited(orch, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    orch.run_codex_planner = lambda goal, atomic=False, project=None: {"jobs": ["from codex"]}
    assert orch.run_planner("add a thing") == {"jobs": ["from codex"]}
    assert orch.agent_cli.claude_cooling_down(orch.r)


def test_planner_other_failure_raises(orch, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "budget")
    orch.run_codex_planner = lambda *a, **k: pytest.fail("must not fall back")
    with pytest.raises(RuntimeError, match="claude planner failed"):
        orch.run_planner("add a thing")


def test_repair_prompt_gets_findings_from_a_claude_reviewer(orch, tmp_path):
    log = tmp_path / "rv.json"
    log.write_text(json.dumps({"type": "result", "result": "BLOCKING: x is wrong\nVERDICT: CHANGES_REQUIRED"}))
    assert "BLOCKING: x is wrong" in orch.review_findings({"log": str(log)})


def test_successful_answer_about_login_or_rate_limits_is_not_unavailability(agent_cli, fake_claude, tmp_path, monkeypatch):
    # A review of auth/rate-limit code must not be mistaken for Claude's own limit.
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "The login handler lacks a rate limit.\nVERDICT: CHANGES_REQUIRED")
    run = agent_cli.run_claude("reviewer", "x", tmp_path, tmp_path / "l.json", 30)
    assert run.ok and not run.unavailable


# --- the dashboard shows what actually runs -------------------------------------------

def test_worker_heartbeat_shows_the_running_jobs_provider_and_model(worker, tmp_path):
    beats = []
    original = worker.heartbeat

    def spy(status="idle"):
        original(status)
        beats.append(dict(worker.redis.records[worker.worker_key()]))

    worker.heartbeat = spy
    worker._process_job = lambda raw: worker.run_agent(json.loads(raw), tmp_path, tmp_path / "rv.json")
    worker.process_job(json.dumps(job("reviewer")))
    working = [b for b in beats if b["status"] == "working" and b["provider"] == "claude"]
    assert working and working[-1]["model"] == "sonnet" and working[-1]["role"] == "reviewer"
    worker.heartbeat()
    idle = worker.redis.records[worker.worker_key()]
    assert idle["role"] == "any" and idle["provider"] == "per role"
    assert "reviewer claude/sonnet" in idle["model"] and "builder codex/" in idle["model"]


def test_orchestrator_heartbeat_and_goal_record_the_planner(orch, fake_claude, monkeypatch):
    orch.heartbeat()
    beat = orch.r.records[f"sid:orchestrators:{orch.ORCHESTRATOR_ID}"]
    assert (beat["provider"], beat["model"]) == ("claude", "opus")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", '{"jobs": []}')
    info = {}
    orch.run_planner("goal", info=info)
    assert info == {"provider": "claude", "model": "opus", "cost_usd": "0.0421"}
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    orch.run_codex_planner = lambda goal, atomic=False, project=None: {"jobs": []}
    info = {}
    orch.run_planner("goal", info=info)
    assert info["provider"] == "codex" and "session limit" in info["fallback"]


# --- concurrency cap on Claude ------------------------------------------------------

def test_slots_are_capped_leased_and_released(agent_cli, monkeypatch):
    monkeypatch.setenv("CLAUDE_MAX_CONCURRENT", "2")
    fake = MemoryRedis()
    assert agent_cli.acquire_claude_slot(fake, "a", 100, now=1000)
    assert agent_cli.acquire_claude_slot(fake, "b", 100, now=1000)
    assert not agent_cli.acquire_claude_slot(fake, "c", 100, now=1000), "cap reached"
    assert agent_cli.acquire_claude_slot(fake, "a", 100, now=1000), "re-acquire by holder is fine"
    agent_cli.release_claude_slot(fake, "a")
    assert agent_cli.acquire_claude_slot(fake, "c", 100, now=1000)
    # b's lease expires (its worker died): the slot comes back by itself
    assert agent_cli.acquire_claude_slot(fake, "d", 100, now=1101)
    assert fake.get(agent_cli.SLOT_LIMIT_KEY) == "2"


def test_wait_for_slot_gives_up_after_max_wait(agent_cli, monkeypatch):
    monkeypatch.setenv("CLAUDE_MAX_CONCURRENT", "1")
    fake = MemoryRedis()
    agent_cli.acquire_claude_slot(fake, "busy", 1000)
    ticks = []
    assert not agent_cli.wait_for_claude_slot(fake, "me", 100, 0, tick=lambda: ticks.append(1),
                                              sleep=lambda s: None)
    agent_cli.release_claude_slot(fake, "busy")
    assert agent_cli.wait_for_claude_slot(fake, "me", 100, 0, sleep=lambda s: None)


def test_job_waits_for_a_slot_then_runs_on_claude_and_frees_it(worker, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_MAX_CONCURRENT", "1")
    worker.agent_cli.acquire_claude_slot(worker.redis, "job:other", 1000)
    freed = []

    def free_after_first_tick():
        if not freed:
            worker.agent_cli.release_claude_slot(worker.redis, "job:other")
            freed.append(1)

    worker.heartbeat = lambda status="idle": free_after_first_tick()
    worker.agent_cli.POLL_SECONDS = 0.01
    worker.run_agent(job("reviewer"), tmp_path, tmp_path / "rv.json")
    assert worker.redis.records["sid:jobs:reviewer1"]["provider"] == "claude"
    assert worker.redis.zsets[worker.agent_cli.SLOTS_KEY] == {}, "slot released after the run"


def test_job_falls_back_to_codex_when_claude_stays_at_capacity(worker, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("CLAUDE_MAX_CONCURRENT", "1")
    monkeypatch.setenv("CLAUDE_SLOT_WAIT_SECONDS", "0")
    worker.agent_cli.acquire_claude_slot(worker.redis, "job:other", 1000)
    worker.run_agent(job("repair"), tmp_path, tmp_path / "r.json")
    record = worker.redis.records["sid:jobs:repair1"]
    assert record["provider"] == "codex" and "at capacity" in record["provider_fallback"]
    assert fake_claude() == []


def test_slot_released_even_when_claude_fails(worker, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "budget")
    with pytest.raises(RuntimeError):
        worker.run_agent(job("repair"), tmp_path, tmp_path / "r.json")
    assert worker.redis.zsets[worker.agent_cli.SLOTS_KEY] == {}


def test_planner_shares_the_cap(orch, monkeypatch, fake_claude):
    monkeypatch.setenv("CLAUDE_MAX_CONCURRENT", "1")
    monkeypatch.setenv("PLANNER_SLOT_WAIT_SECONDS", "0")
    orch.agent_cli.acquire_claude_slot(orch.r, "job:busy", 1000)
    orch.run_codex_planner = lambda goal, atomic=False, project=None: {"jobs": ["codex"]}
    info = {}
    assert orch.run_planner("g", info=info) == {"jobs": ["codex"]}
    assert "at capacity" in info["fallback"] and fake_claude() == []



# --- specialist reviews ------------------------------------------------------------------

@pytest.fixture
def aspects(worker, monkeypatch):
    monkeypatch.setenv("REVIEW_ASPECTS", "spec,safety")
    return worker


def test_specialist_reviews_run_in_parallel_and_all_must_pass(aspects, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("FAKE_ASPECT_SPEC", "req 1 MET\nVERDICT: PASS")
    monkeypatch.setenv("FAKE_ASPECT_SAFETY", "no issues\nVERDICT: PASS_WITH_NOTES")
    log = tmp_path / "rv.json"
    assert aspects.run_agent(job("reviewer"), tmp_path, log)[0] == 0
    calls = fake_claude()
    models = sorted(flag(c["argv"], "--model") for c in calls)
    assert models == ["claude-haiku-4-5-20251001", "sonnet"], "spec on sonnet, safety on haiku"
    assert all(flag(c["argv"], "--tools") == "Read,Grep,Glob" for c in calls)
    verdict, findings = aspects.parse_review_verdict(aspects.agent_cli.agent_messages(log))
    assert verdict == "pass"
    assert "## spec review (sonnet)" in findings and "## safety review" in findings
    assert aspects.redis.records["sid:jobs:reviewer1"]["cost_usd"] == "0.0842", "costs summed"
    assert aspects.parse_codex_log(log)[1]["effective_tokens"] == "520", "usage summed"


def test_any_blocking_aspect_blocks_the_review(aspects, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ASPECT_SPEC", "all MET\nVERDICT: PASS")
    monkeypatch.setenv("FAKE_ASPECT_SAFETY", "BLOCKING: token logged\nVERDICT: CHANGES_REQUIRED")
    log = tmp_path / "rv.json"
    aspects.run_agent(job("reviewer"), tmp_path, log)
    text = aspects.agent_cli.agent_messages(log)[0]
    assert aspects.parse_review_verdict([text])[0] == "changes_required"
    assert "Blocking findings from: safety" in text
    assert text.count("VERDICT:") == 1, "aspect verdict lines are relabelled"


def test_aspect_without_verdict_fails_the_review(aspects, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ASPECT_SPEC", "VERDICT: PASS")
    monkeypatch.setenv("FAKE_ASPECT_SAFETY", "I looked around.")
    with pytest.raises(RuntimeError, match="no verdict: safety"):
        aspects.run_agent(job("reviewer"), tmp_path, tmp_path / "rv.json")


def test_aspect_hitting_plan_limit_falls_back_to_one_codex_review(aspects, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "aspect_limit")
    monkeypatch.setenv("FAKE_ASPECT_SPEC", "VERDICT: PASS")
    aspects.run_agent(job("reviewer"), tmp_path, tmp_path / "rv.json")
    assert aspects.codex_calls == ["reviewer1"]
    assert aspects.agent_cli.claude_cooling_down(aspects.redis)
    assert aspects.redis.zsets[aspects.agent_cli.SLOTS_KEY] == {}, "one slot, released"


def test_single_aspect_is_a_plain_review(worker, tmp_path, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "VERDICT: PASS")
    worker.run_agent(job("reviewer"), tmp_path, tmp_path / "rv.json")
    assert len(fake_claude()) == 1


def test_unknown_aspect_is_a_configuration_error(agent_cli, monkeypatch):
    monkeypatch.setenv("REVIEW_ASPECTS", "spec,vibes")
    with pytest.raises(ValueError, match="vibes"):
        agent_cli.review_aspects()


def test_aspect_models_are_overridable(agent_cli, monkeypatch):
    assert agent_cli.aspect_model("spec") == "sonnet"
    assert agent_cli.aspect_model("safety") == "claude-haiku-4-5-20251001"
    monkeypatch.setenv("CLAUDE_REVIEW_SAFETY_MODEL", "sonnet")
    assert agent_cli.aspect_model("safety") == "sonnet"



# --- cheaper Claude usage ----------------------------------------------------------------

def test_rebase_check_is_one_cheap_fresh_review(worker, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("REVIEW_ASPECTS", "spec,safety")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "still fine\nVERDICT: PASS")
    worker.run_agent(job("reviewer", rebase_check=True), tmp_path, tmp_path / "rv.json")
    [call] = fake_claude()
    assert flag(call["argv"], "--model") == "claude-haiku-4-5-20251001"
    assert flag(call["argv"], "--tools") == "", "no tools: one turn, no exploration"
    assert "REBASE CHECK" in call["prompt"]
    assert worker.redis.records["sid:jobs:reviewer1"]["review_kind"] == "rebase_check"


def test_changed_patch_gets_the_full_specialist_review(worker, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("REVIEW_ASPECTS", "spec,safety")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "VERDICT: PASS")
    worker.run_agent(job("reviewer", rebase_check=False), tmp_path, tmp_path / "rv.json")
    assert len(fake_claude()) == 2


def test_patch_id_ignores_the_base(worker, tmp_path):
    import subprocess as sp
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: sp.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q", "-b", "main")
    (repo / "a.txt").write_text("1\n")
    (repo / "b.txt").write_text("x\n")
    g("add", "."); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    base1 = g("rev-parse", "HEAD")
    (repo / "a.txt").write_text("1\n2\n")
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "change"], cwd=repo, check=True)
    cand1 = g("rev-parse", "HEAD")
    # same change on a newer, unrelated base
    g("checkout", "-q", base1); (repo / "b.txt").write_text("y\n")
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "main moved"], cwd=repo, check=True)
    base2 = g("rev-parse", "HEAD")
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "cherry-pick", cand1], cwd=repo, check=True, capture_output=True)
    cand2 = g("rev-parse", "HEAD")
    worker.run_git = lambda *a, cwd=None, check=True: sp.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    first = worker.candidate_patch_id(base1, cand1, repo)
    assert first and first == worker.candidate_patch_id(base2, cand2, repo)
    assert worker.candidate_patch_id(base1, base1, repo) == ""


def test_only_a_passing_review_vouches_for_the_patch():
    source = (ROOT / "services/worker/worker.py").read_text()
    assert '"reviewed_patch_id": str(job.get("patch_id") or "") if verdict == "pass" else ""' in source


def test_atomic_goals_plan_on_sonnet(orch, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", '{"jobs": []}')
    info = {}
    orch.run_planner("one thing", atomic=True, info=info)
    assert flag(fake_claude()[-1]["argv"], "--model") == "sonnet" and info["model"] == "sonnet"
    orch.run_planner("many things", atomic=False, info=info)
    assert flag(fake_claude()[-1]["argv"], "--model") == "opus"


# --- documentation reviews converge ----------------------------------------------------

def test_docs_only_changes_get_the_documentation_bar(worker, tmp_path):
    import subprocess as sp
    repo = tmp_path / "d"
    repo.mkdir()
    g = lambda *a: sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=repo,
                          check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q", "-b", "main")
    (repo / "README.md").write_text("a\n")
    (repo / "app.py").write_text("x = 1\n")
    g("add", "."); g("commit", "-qm", "base")
    base = g("rev-parse", "HEAD")
    (repo / "docs").mkdir()
    (repo / "docs" / "setup.md").write_text("guide\n")
    (repo / "README.md").write_text("b\n")
    g("add", "."); g("commit", "-qm", "docs")
    docs = g("rev-parse", "HEAD")
    (repo / "app.py").write_text("x = 2\n")
    g("commit", "-qam", "code")
    code = g("rev-parse", "HEAD")
    worker.run_git = lambda *a, cwd=None, check=True: sp.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    assert worker.is_docs_only(base, docs, repo) is True
    assert worker.is_docs_only(base, code, repo) is False, "any code file means the code bar"
    assert worker.is_docs_only(docs, docs, repo) is False, "empty change is not docs-only"
    assert "BLOCKING only for statements that are factually wrong" in worker.DOCS_REVIEW_BAR


def test_review_prompt_appends_docs_bar_only_for_docs():
    source = (ROOT / "services/worker/worker.py").read_text()
    assert "    if docs_only:\n        prompt += DOCS_REVIEW_BAR" in source



def test_rebase_check_prompt_carries_main_changes(worker, tmp_path):
    import subprocess as sp
    repo = tmp_path / "m"
    repo.mkdir()
    g = lambda *a: sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=repo,
                          check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q", "-b", "main")
    (repo / "api.py").write_text("def f():\n    return 1\n")
    g("add", "."); g("commit", "-qm", "old base")
    old = g("rev-parse", "HEAD")
    (repo / "api.py").write_text("def f():\n    return 2\n")
    g("commit", "-qam", "main moved")
    new = g("rev-parse", "HEAD")
    worker.run_git = lambda *a, cwd=None, check=True: sp.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    packet = worker.main_changes_packet(old, new, repo)
    assert "api.py" in packet and "+    return 2" in packet and old[:12] in packet


def test_passing_review_records_its_base():
    source = (ROOT / "services/worker/worker.py").read_text()
    assert '"reviewed_base_commit": str(builder.get("integration_base_commit") or "") if verdict == "pass" else ""' in source



# --- the claude CLI updating itself under the pipeline ---------------------------------

def test_missing_executable_is_a_short_outage_not_a_failure(agent_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # no claude anywhere
    run = agent_cli.run_claude("reviewer", "x", tmp_path, tmp_path / "l.json", 30)
    assert run.unavailable and run.install_broken and not run.ok
    fake = MemoryRedis()
    agent_cli.start_cooldown(fake, f"claude unavailable: {run.describe_error()}")
    assert fake.values[agent_cli.COOLDOWN_KEY]
    assert agent_cli.INSTALL_COOLDOWN_SECONDS == 120


def test_half_installed_cli_is_unavailable(agent_cli, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    broken = bin_dir / "claude"
    broken.write_text("#!/bin/sh\necho 'Either postinstall did not run (--ignore-scripts)'\nexit 1\n")
    broken.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    run = agent_cli.run_claude("reviewer", "x", tmp_path, tmp_path / "l.json", 30)
    assert run.unavailable and "postinstall" in run.describe_error()


def test_plan_limit_still_gets_the_long_cooldown(agent_cli):
    seen = {}

    class R:
        def set(self, key, value, ex=None):
            seen["ex"] = ex

    agent_cli.start_cooldown(R(), "claude unavailable: success: You've hit your session limit")
    assert seen["ex"] == agent_cli.COOLDOWN_SECONDS
    agent_cli.start_cooldown(R(), "claude unavailable: [Errno 2] No such file or directory: 'claude'")
    assert seen["ex"] == agent_cli.INSTALL_COOLDOWN_SECONDS


def test_crash_with_unrelated_output_is_still_a_failure(agent_cli, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    run = agent_cli.run_claude("repair", "x", tmp_path, tmp_path / "l.json", 30)
    assert not run.unavailable and "Traceback" in run.describe_error()


def test_planner_falls_back_when_claude_is_mid_update(orch, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    orch.run_codex_planner = lambda goal, atomic=False, project=None: {"jobs": ["codex"]}
    info = {}
    assert orch.run_planner("g", info=info) == {"jobs": ["codex"]}
    assert info["provider"] == "codex" and "No such file" in info["fallback"]
