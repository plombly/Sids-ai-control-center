"""Why a job is waiting, and pausing all new work."""

import why_routes
from test_teams import team  # noqa: F401  (fixture)

NOW_STATE = {"paused": False, "held": {}, "workers": [{"status": "busy"}], "queue": ["j2", "j1"],
             "claude_full": False, "statuses": {"d1": "running"}}


def test_each_waiting_state_gets_a_plain_reason():
    explain = lambda job, **over: why_routes.explain(job, {**NOW_STATE, **over})
    assert explain({"id": "j1", "status": "queued"}) == (True, "Every worker is busy (number 2 in line); it starts as soon as one is free.")
    assert "a free worker picks it up" in explain({"id": "j1", "status": "queued"}, workers=[{"status": "idle"}])[1]
    assert "Codex is paused until" in explain({"id": "j1", "status": "queued"}, held={"codex": 2e9})[1]
    assert "Waiting for 1 earlier job" in explain({"status": "blocked", "dependencies": '["d1"]'})[1]
    assert "files another job" in explain({"status": "blocked", "blocked_reason": "waiting for a.py held by job x"})[1]
    assert "worker was lost" in explain({"status": "deferred", "defer_kind": "interrupted"})[1]
    assert "Waiting for you" in explain({"status": "needs_human", "needs_human_reason": "tests keep failing"})[1]
    assert explain({"status": "awaiting_review", "review_status": "complete", "review_verdict": "pass"})[1] == "Ready: waiting for your approval."
    assert "paused (Settings" in explain({"status": "queued"}, paused=True)[1]
    assert explain({"status": "running"}) == (False, "being worked on now")


def test_pause_is_admin_only_and_round_trips(team):
    admin, fake = team
    assert admin.get("/api/system/pause").json() == {"paused": False}
    assert admin.post("/api/system/pause", json={"paused": True, "reason": "maintenance"}).json()["paused"] is True
    assert admin.get("/api/system/pause").json()["reason"] == "maintenance"
    assert admin.post("/api/system/pause", json={"paused": False}).json() == {"paused": False}


def test_a_review_or_repair_waiting_for_a_provider_says_so():
    from why_routes import explain
    state = {"held": {"codex": 1792400000}}
    _, reason = explain({"status": "awaiting_review", "review_status": "failed", "review_wait_provider": "codex"}, state)
    assert reason.startswith("Its review waits for codex") and "no attempt is used" in reason
    _, reason = explain({"status": "awaiting_review", "review_status": "complete", "review_verdict": "changes_required",
                         "repair_status": "waiting", "repair_wait_provider": "codex"}, state)
    assert reason.startswith("Its repair waits for codex")
