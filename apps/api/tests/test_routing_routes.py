"""Routing summary (apps/api/routing_routes.py)."""

import routing_routes


def test_runs_per_role_switches_and_waits():
    jobs = [{"id": "a", "role": "builder", "provider": "codex", "model": "gpt", "created_at": "100", "deferrals": "2"},
            {"id": "b", "role": "reviewer", "provider": "codex", "model": "gpt", "created_at": "200",
             "provider_fallback": "claude unavailable: quota"},
            {"id": "c", "role": "reviewer", "provider": "claude", "model": "sonnet", "created_at": "300"},
            {"id": "old", "role": "builder", "provider": "codex", "created_at": "1"}]
    out = routing_routes.summarize(jobs, since=50)
    reviewer = next(r for r in out["roles"] if r["role"] == "reviewer")
    assert {(x["provider"], x["count"]) for x in reviewer["runs"]} == {("codex", 1), ("claude", 1)}
    assert out["fallbacks"] == [{"job": "b", "role": "reviewer", "at": 200.0, "reason": "claude unavailable: quota"}]
    assert out["waits"] == {"provider_waits": 2}


def test_runs_and_moves_per_account():
    from routing_routes import summarize
    jobs = [{"id": "a", "role": "reviewer", "provider": "claude", "provider_account": "default", "created_at": "10"},
            {"id": "b", "role": "reviewer", "provider": "claude", "provider_account": "work", "created_at": "10",
             "account_switches": "1"},
            {"id": "c", "role": "builder", "provider": "codex", "provider_account": "default", "created_at": "10"}]
    result = summarize(jobs, 0)
    assert {(a["provider"], a["account"], a["count"]) for a in result["accounts"]} == {
        ("claude", "default", 1), ("claude", "work", 1), ("codex", "default", 1)}
    assert result["waits"]["account_switches"] == 1
