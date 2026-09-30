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
    print(json.dumps({**base, "is_error": False, "subtype": "success",
                      "result": os.environ.get("FAKE_CLAUDE_RESULT", "done")}))
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
def worker(tmp_path, fake_claude):
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
    module.repository_manifest = lambda: "apps/api/main.py"
    return module


def test_planner_uses_claude_opus_read_only(orch, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", 'Here:\n```json\n{"jobs": [{"title": "t"}]}\n```')
    assert orch.run_planner("add a thing") == {"jobs": [{"title": "t"}]}
    argv = fake_claude()[0]["argv"]
    assert flag(argv, "--model") == "opus"
    assert flag(argv, "--tools") == "Read,Grep,Glob"


def test_planner_falls_back_to_codex_when_claude_is_limited(orch, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    orch.run_codex_planner = lambda goal, atomic=False: {"jobs": ["from codex"]}
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
