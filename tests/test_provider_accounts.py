"""Several Claude / Codex accounts: runs spread over them, holds per account,
a provider paused only when every account is, credentials per account."""

import json
import os
import sys

import pytest

from laika_testing import MemoryRedis, ROOT, load_module

sys.path.insert(0, str(ROOT / "services"))
import provider_accounts  # noqa: E402
import provider_health  # noqa: E402


@pytest.fixture(autouse=True)
def signed_in_home(tmp_path, monkeypatch):
    """Extra accounts are used only with credentials: every test account has them."""
    monkeypatch.setattr(provider_accounts, "credential_home", lambda: tmp_path / "laika-home")
    return tmp_path / "laika-home"


def add(r, provider, account, limit=2, enabled="1", kind="login", signed_in=True):
    if signed_in:
        base = provider_accounts.credential_home()
        if provider == "claude":
            path = provider_accounts.claude_token_path(account, base)
        else:
            path = provider_accounts.codex_home(account, base) / "auth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    r.sadd(f"laika:provider-accounts:{provider}", account)
    r.hset(f"laika:provider-account:{provider}:{account}",
           mapping={"label": account, "kind": kind, "max_concurrent": str(limit), "enabled": enabled})


def test_without_extra_accounts_everything_behaves_as_before(monkeypatch):
    monkeypatch.setenv("CLAUDE_MAX_CONCURRENT", "2")
    r = MemoryRedis()
    assert [a["id"] for a in provider_accounts.accounts(r, "claude")] == ["default"]
    assert provider_accounts.acquire(r, "claude", "a", 100, now=1000) == "default"
    assert provider_accounts.acquire(r, "claude", "b", 100, now=1000) == "default"
    assert provider_accounts.acquire(r, "claude", "c", 100, now=1000) is None      # the old cap
    assert provider_accounts.running_key("claude", "default") == "laika:provider-slots:claude"
    assert provider_accounts.acquire(r, "codex", "x", 100, now=1000) == "default"  # Codex: no limit


def test_runs_go_to_the_account_with_the_most_room(monkeypatch):
    monkeypatch.setenv("CLAUDE_MAX_CONCURRENT", "2")
    r = MemoryRedis()
    add(r, "claude", "work", limit=4)
    picks = [provider_accounts.acquire(r, "claude", f"h{i}", 100, now=1000) for i in range(6)]
    assert picks.count("default") == 2 and picks.count("work") == 4
    assert picks[:2] == ["default", "work"]            # an even spread, not one filled first
    assert provider_accounts.acquire(r, "claude", "h7", 100, now=1000) is None
    assert provider_accounts.capacity(r, "claude", now=1000) == (0, 6)


def test_an_account_that_is_not_signed_in_is_never_used():
    r = MemoryRedis()
    add(r, "codex", "new", signed_in=False)
    assert provider_accounts.candidates(r, "codex") == ["default"]
    provider_health.hold(r, "codex", provider_health.Failure("quota", "usage limit"))
    assert provider_health.held(r, "codex")          # nothing else can serve: the provider pauses


def test_lower_plan_usage_wins_a_tie(monkeypatch):
    r = MemoryRedis()
    add(r, "claude", "work")
    r.set("laika:provider-usage:claude", json.dumps({"windows": {"five_hour": {"percent": 70}}}))
    r.set("laika:provider-usage:claude:work", json.dumps({"windows": {"five_hour": {"percent": 10}}}))
    assert provider_accounts.candidates(r, "claude", now=1000) == ["work", "default"]


def test_a_held_or_disabled_account_is_skipped_and_the_provider_pauses_only_when_all_are():
    r = MemoryRedis()
    add(r, "codex", "second")
    add(r, "codex", "spare", enabled="")
    limit = provider_health.Failure("quota", "usage limit", until=None)
    provider_health.hold(r, "codex", limit)                      # the default account
    assert provider_health.held(r, "codex", "default") and not provider_health.held(r, "codex")
    assert provider_accounts.candidates(r, "codex") == ["second"]
    assert not r.get("laika:provider-cooldown:codex")             # no provider-wide pause yet
    provider_health.hold(r, "codex", limit, account="second")
    assert provider_health.held(r, "codex") and r.get("laika:provider-cooldown:codex")
    assert provider_health.info(r, "codex")["held"] and provider_accounts.acquire(r, "codex", "x", 10) is None
    provider_health.release(r, "codex", "second")                 # e.g. signed in again
    assert not provider_health.held(r, "codex") and provider_accounts.candidates(r, "codex") == ["second"]


def test_credentials_per_account(tmp_path):
    home = tmp_path
    (home / ".config/laika/claude-tokens").mkdir(parents=True)
    (home / ".config/laika/claude-tokens/work").write_text("sk-ant-oat01-" + "w" * 40 + "\n")  # release-scan: allow (fake)
    (home / ".config/laika/claude-keys").mkdir(parents=True)
    (home / ".config/laika/claude-keys/paid").write_text("sk-ant-api03-" + "p" * 40)  # release-scan: allow (fake)
    work = provider_accounts.env_for("claude", "work", base=home)
    assert work["CLAUDE_CODE_OAUTH_TOKEN"].endswith("w" * 40) and work["ANTHROPIC_API_KEY"] == ""
    paid = provider_accounts.env_for("claude", "paid", base=home)
    assert paid["ANTHROPIC_API_KEY"].endswith("p" * 40) and paid["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    env = provider_accounts.apply_env({"ANTHROPIC_API_KEY": "default-key", "HOME": "/h"}, work)
    assert "ANTHROPIC_API_KEY" not in env and env["CLAUDE_CODE_OAUTH_TOKEN"]
    assert provider_accounts.env_for("codex", "second", base=home) == {"CODEX_HOME": str(home / ".codex-accounts/second")}
    assert provider_accounts.env_for("codex", "default", base=home) == {}


@pytest.fixture
def worker(monkeypatch, tmp_path):
    module = load_module(ROOT / "services/worker/worker.py", "worker_accounts_test")
    module.redis = MemoryRedis()
    module.heartbeat = lambda *a, **k: None
    module.agent_sandbox = lambda worktree: None
    monkeypatch.setenv("ROLE_PROVIDERS", "reviewer=claude")
    monkeypatch.setattr(module.agent_cli, "review_aspects", lambda: ["spec"])
    return module


class Run:
    def __init__(self, account, unavailable=False):
        self.account, self.unavailable = account, unavailable
        self.ok, self.timed_out = not unavailable, False
        self.result = {"total_cost_usd": 0.01, "usage": {}} if not unavailable else None
        self.failure = provider_health.Failure("quota", "You've hit your limit") if unavailable else None
        self.duration = 1.0

    def describe_error(self):
        return "You've hit your limit"


def test_a_claude_account_at_its_limit_hands_the_run_to_another_account(worker, tmp_path, monkeypatch):
    add(worker.redis, "claude", "work")
    used = []

    def run_claude(role, prompt, cwd, log_path, timeout, account=None, **kwargs):
        used.append(account)
        return Run(account, unavailable=(account == "default"))

    monkeypatch.setattr(worker.agent_cli, "run_claude", run_claude)
    job = {"id": "rv1", "role": "reviewer", "prompt": "review"}
    worker.redis.records["laika:jobs:rv1"] = dict(job)
    assert worker.run_agent(job, tmp_path, tmp_path / "rv.json")[0] == 0
    record = worker.redis.records["laika:jobs:rv1"]
    assert used == ["default", "work"] and record["provider"] == "claude" and record["provider_account"] == "work"
    assert record["account_switches"] == "1"
    assert provider_health.held(worker.redis, "claude", "default") and not provider_health.held(worker.redis, "claude")
    assert all(not z for z in worker.redis.zsets.values())        # every slot released


def test_codex_runs_as_the_chosen_account_and_a_limit_on_one_is_a_switch(worker, tmp_path, monkeypatch):
    add(worker.redis, "codex", "second", limit=0)
    seen = []

    def run_codex(job, worktree, log_path, account=None):
        seen.append(account)
        log_path.write_text(json.dumps({"type": "turn.failed", "error": {
            "message": "You've hit your usage limit. Try again in 3 hours."}}) + "\n")
        return 1, 1.0

    worker.run_codex = run_codex
    job = {"id": "b1", "role": "builder", "prompt": "build"}
    worker.redis.records["laika:jobs:b1"] = dict(job)
    with pytest.raises(worker.ProviderWait) as wait:
        worker.codex_or_wait(job, tmp_path, tmp_path / "b.json")
    assert wait.value.switch                                      # the other account is still free
    first = seen[0]
    assert provider_health.held(worker.redis, "codex", first) and not provider_health.held(worker.redis, "codex")
    with pytest.raises(worker.ProviderWait) as wait:
        worker.codex_or_wait(job, tmp_path, tmp_path / "b.json")
    assert seen[1] != first and not wait.value.switch            # now every account waits


def test_a_switch_does_not_use_up_the_provider_waits():
    orch = load_module(ROOT / "services/orchestrator/orchestrator.py", "orch_accounts_test")
    orch.r = MemoryRedis()
    orch.r.records["laika:jobs:b1"] = {
        "id": "b1", "role": "builder", "status": "deferred", "goal_id": "g1", "prompt": "x", "build_attempt": "1",
        "defer_kind": "provider", "defer_provider": "codex", "deferrals": "0", "account_switches": "1"}
    orch.resume_deferred_jobs()
    assert orch.r.records["laika:jobs:b1"]["status"] == "queued"
    orch.r.records["laika:jobs:b1"].update(status="deferred", defer_kind="provider", account_switches="2")
    orch.resume_deferred_jobs()                                   # a new round: resumed again
    assert orch.r.records["laika:jobs:b1"]["status"] == "queued"


def test_the_host_puts_extra_accounts_keys_in_place_and_removes_deleted_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    helper = load_module(ROOT / "scripts/laika-providers.py", "providers_accounts_test")
    r = MemoryRedis()
    add(r, "claude", "paid", kind="key")
    add(r, "codex", "second", kind="key")
    stale = tmp_path / ".codex-accounts/gone"
    stale.mkdir(parents=True)
    (tmp_path / ".config/laika/claude-tokens").mkdir(parents=True)
    (tmp_path / ".config/laika/claude-tokens/old").write_text("x")
    (tmp_path / ".config/laika/claude-token").write_text("default stays")
    calls = []

    def runner(argv, stdin=None, env=None, **kwargs):
        calls.append((argv, (env or {}).get("CODEX_HOME")))
        return helper.subprocess.CompletedProcess(argv, 0, "Logged in using an API key", "")

    environ = {"ANTHROPIC_API_KEY__PAID": "sk-ant-api03-" + "k" * 30,  # release-scan: allow (fake)
               "OPENAI_API_KEY__SECOND": "sk-" + "o" * 30}  # release-scan: allow (fake)
    helper.status = lambda r, runner=None: {}
    helper.apply_keys(r, runner=runner, environ=environ, base=tmp_path)
    key_file = tmp_path / ".config/laika/claude-keys/paid"
    assert key_file.read_text().strip().endswith("k" * 30) and oct(key_file.stat().st_mode)[-3:] == "600"
    assert (["codex", "login", "--with-api-key"], str(tmp_path / ".codex-accounts/second")) in calls
    assert not stale.exists() and not (tmp_path / ".config/laika/claude-tokens/old").exists()
    assert (tmp_path / ".config/laika/claude-token").exists()
    assert helper.account_key_name("claude", "my-work") == "ANTHROPIC_API_KEY__MY_WORK"


def test_sandboxes_never_see_extra_accounts_keys(monkeypatch, tmp_path):
    import project_sandbox
    monkeypatch.setenv("ANTHROPIC_API_KEY__PAID", "secret")
    monkeypatch.setattr(project_sandbox, "enabled", lambda: True)
    project = type("P", (), {"is_builtin": False, "root": tmp_path, "repo": tmp_path / "repo", "id": "p"})()
    (tmp_path / "repo").mkdir()
    for kind in ("agent", "gate"):
        argv = project_sandbox.command(["true"], project, tmp_path / "repo", kind=kind)
        assert "ANTHROPIC_API_KEY__PAID" in argv[argv.index("--unsetenv"):]
