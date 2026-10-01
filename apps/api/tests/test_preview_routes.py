"""Previews: only ready changes of projects with a run command."""

import pytest
from fastapi.testclient import TestClient

import main
from test_project_routes import FakeRedis


def ready(**extra):
    return {"id": "j1", "project_id": "shop", "status": "awaiting_review", "review_status": "complete",
            "review_verdict": "pass", "integration_status": "passed", "review_job_id": "r1",
            "integrated_candidate_commit": "c" * 40, "integration_base_commit": "b" * 40,
            "reviewed_commit": "c" * 40, **extra}


@pytest.fixture
def client(monkeypatch):
    fake = FakeRedis({"sid:jobs:j1": ready(), "sid:jobs:r1": {"id": "r1", "status": "review_complete",
                                                              "reviewed_commit": "c" * 40},
                      "sid:projects:shop": {"id": "shop", "run_command": "npm start", "status": "active"}},
                     members={"shop"})
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(main, "_approval_ready", lambda job: job.get("status") == "awaiting_review")
    with TestClient(main.app) as test_client:
        yield test_client, fake


def test_start_and_stop_a_preview(client):
    test_client, fake = client
    started = test_client.post("/api/jobs/j1/preview")
    assert started.status_code == 202 and started.json()["state"] == "requested"
    assert fake.hashes["sid:preview:j1"]["candidate"] == "c" * 40
    assert test_client.post("/api/jobs/j1/preview").json()["state"] == "requested"  # idempotent
    approvals = test_client.get("/api/approvals").json()
    assert approvals[0]["previewable"] is True and approvals[0]["preview"]["state"] == "requested"
    assert test_client.delete("/api/jobs/j1/preview").json()["state"] == "stop"


def test_previews_need_a_ready_change_and_a_run_command(client):
    test_client, fake = client
    fake.hashes["sid:projects:shop"]["run_command"] = ""
    assert test_client.post("/api/jobs/j1/preview").status_code == 409
    fake.hashes["sid:projects:shop"]["run_command"] = "npm start"
    fake.hashes["sid:jobs:j1"]["status"] = "merged"
    assert test_client.post("/api/jobs/j1/preview").status_code == 409
    assert test_client.post("/api/jobs/nope/preview").status_code == 404
    assert test_client.delete("/api/jobs/j1/preview").status_code == 404
