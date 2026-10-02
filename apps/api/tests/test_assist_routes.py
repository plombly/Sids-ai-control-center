"""Goal assistant API: sessions, replies, cancel and submit."""

import pytest
from fastapi.testclient import TestClient

import goal_assist
import main
from test_project_routes import FakeRedis


class AssistRedis(FakeRedis):
    def expire(self, key, seconds):
        return True

    def lrange(self, key, start, end):
        return [v for k, v in self.queues if k == key]


@pytest.fixture
def api(monkeypatch):
    fake = AssistRedis({"sid:projects:game": {"id": "game", "name": "Game", "status": "active"},
                        "sid:projects:old": {"id": "old", "status": "archived"}}, members=["game", "old"])
    monkeypatch.setattr(main, "redis", fake)
    submitted = []
    monkeypatch.setattr(main, "_submit_goal", lambda payload, project_id="sid": submitted.append((payload, project_id)) or {"id": "g1", "status": "accepted"})
    return TestClient(main.app), fake, submitted


def test_a_session_runs_from_idea_to_submitted_goal(api):
    client, fake, submitted = api
    started = client.post("/api/projects/game/assistant", json={"idea": "  add a pause menu "})
    assert started.status_code == 202
    session = started.json()
    sid = session["id"]
    assert session["status"] == "queued" and session["turns"] == [{"from": "you", "idea": "add a pause menu"}]
    assert fake.lrange(goal_assist.QUEUE_KEY, 0, -1) == [sid]
    # Nothing to answer or revise yet.
    assert client.post(f"/api/assistant/{sid}/reply", json={"answers": ["x"]}).status_code == 409
    # The host turn asks a question.
    goal_assist.save(fake, sid, status="questions", questions=[{"question": "Key?", "options": ["Esc"]}, {"question": "Sound?"}])
    answered = client.post(f"/api/assistant/{sid}/reply", json={"answers": ["Esc"]}).json()
    assert answered["status"] == "queued" and answered["questions"] == []
    assert answered["turns"][-1] == {"from": "you", "answers": [{"question": "Key?", "answer": "Esc"}, {"question": "Sound?", "answer": ""}]}
    # The host turn writes a brief; the operator asks for a change, then submits an edited version.
    brief = {"title": "Pause", "summary": "s", "goal": "Add a pause menu.", "atomic": True}
    goal_assist.save(fake, sid, status="brief", brief=brief)
    assert client.get(f"/api/assistant/{sid}").json()["brief"] == brief
    assert client.post(f"/api/assistant/{sid}/reply", json={"feedback": "  "}).status_code == 422
    assert client.post(f"/api/assistant/{sid}/reply", json={"feedback": "also P"}).json()["status"] == "queued"
    assert client.post(f"/api/assistant/{sid}/submit", json={"goal": "x", "request_id": "req-12345678"}).status_code == 409
    goal_assist.save(fake, sid, status="brief")
    done = client.post(f"/api/assistant/{sid}/submit", json={"goal": "Add a pause menu (P too).", "atomic": True, "request_id": "req-12345678"})
    assert done.status_code == 202 and done.json()["id"] == "g1"
    payload, project = submitted[0]
    assert project == "game" and payload.goal == "Add a pause menu (P too)." and payload.atomic
    state = client.get(f"/api/assistant/{sid}").json()
    assert (state["status"], state["goal_id"]) == ("submitted", "g1")
    # Submitting twice does not submit twice; cancelling a submitted session changes nothing.
    client.post(f"/api/assistant/{sid}/submit", json={"goal": "y", "request_id": "req-87654321"})
    assert len(submitted) == 1
    assert client.post(f"/api/assistant/{sid}/cancel").json()["status"] == "submitted"


def test_validation_and_limits(api):
    client, fake, _ = api
    assert client.post("/api/projects/nope/assistant", json={"idea": "x"}).status_code == 404
    assert client.post("/api/projects/old/assistant", json={"idea": "x"}).status_code == 409
    assert client.post("/api/projects/game/assistant", json={"idea": "   "}).status_code == 422
    assert client.post("/api/projects/game/assistant", json={"idea": "x" * 4001}).status_code == 422
    assert client.get("/api/assistant/../x").status_code == 404
    assert client.get("/api/assistant/" + "f" * 16).status_code == 404
    for _ in range(6):
        assert client.post("/api/projects/game/assistant", json={"idea": "x"}).status_code == 202
    assert client.post("/api/projects/game/assistant", json={"idea": "x"}).status_code == 429
    sid = client.post("/api/projects/sid/assistant", json={"idea": "x"})
    assert sid.status_code == 403  # the built-in project is view-only
