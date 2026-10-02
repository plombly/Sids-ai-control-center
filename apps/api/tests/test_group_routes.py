"""Approve all of a goal (merge queue), group activity and build-all."""

import json

import pytest

import activity_routes
import main
from test_operator_api import AUTH, CANDIDATE, OperatorFakeRedis, client, heartbeat  # noqa: F401  (fixture)

OTHER = "d" * 40


def ready(job_id, project="shop", candidate=CANDIDATE, goal="g1"):
    return {"id": job_id, "goal_id": goal, "status": "awaiting_review", "project_id": project, "role": "builder",
            "review_status": "complete", "review_verdict": "pass", "integration_status": "passed",
            "review_job_id": f"r-{job_id}", "integrated_candidate_commit": candidate, "integration_base_commit": "b" * 40,
            "review_candidate_commit": candidate, "integration_candidate_commit": candidate}


@pytest.fixture
def fake(monkeypatch):
    fake = OperatorFakeRedis({
        "laika:operator-service:laika-operator-01": heartbeat(),
        "laika:goals:g1": {"id": "g1", "status": "running", "project_id": "shop"},
        "laika:jobs:j1": ready("j1"),
        "laika:jobs:j2": ready("j2", project="shop-api", candidate=OTHER),
        "laika:jobs:j3": {**ready("j3"), "review_verdict": "changes_required"},
        "laika:jobs:j9": ready("j9", goal="other"),
        "laika:projects:app": {"id": "app", "view_only": "1"},
        "laika:jobs:j4": ready("j4", project="app"),
    })
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(main, "OPERATOR_TOKEN", "operator-test-token")
    monkeypatch.setattr(main, "_approval_ready", lambda job: job.get("review_verdict") == "pass"
                        and job.get("status") == "awaiting_review")
    return fake


def approve(client, candidates, request_id="all-00000001"):
    return client.post("/api/goals/g1/approve-all", json={"request_id": request_id, "candidates": candidates},
                       headers=AUTH)


def test_each_change_the_human_saw_is_queued_with_its_exact_candidate(client, fake):
    response = approve(client, {"j1": CANDIDATE, "j2": OTHER})
    assert response.status_code == 202, response.text
    queued = response.json()["queued"]
    assert [q["job_id"] for q in queued] == ["j1", "j2"] and all(q["status"] == "pending" for q in queued)
    sent = [fields for _, fields, _ in fake.stream]
    assert [(f["job_id"], f["action"], f["expected_candidate"]) for f in sent] == [
        ("j1", "queue_approve", CANDIDATE), ("j2", "queue_approve", OTHER)]
    assert sent[0]["request_id"] == "all-00000001-01"


@pytest.mark.parametrize("candidates,problem", [
    ({"j1": OTHER}, "changed since you looked"),
    ({"j3": CANDIDATE}, "not ready"),
    ({"j9": CANDIDATE}, "not a change of this goal"),
    ({"j4": CANDIDATE}, "view-only"),
    ({"j1": "short"}, "full commit id"),
])
def test_anything_wrong_approves_nothing(client, fake, candidates, problem):
    response = approve(client, {**candidates, "j2": OTHER})
    assert response.status_code == 409
    assert any(problem in p for p in response.json()["detail"]["problems"])
    assert fake.stream == []


def test_unknown_goal(client, fake):
    assert client.post("/api/goals/nope/approve-all", json={"request_id": "all-00000002", "candidates": {"j1": CANDIDATE}},
                       headers=AUTH).status_code == 404


def test_group_timeline_names_each_project():
    goals = {"g1": {"project_id": "shop", "goal": "Add cart", "created_at": "10"},
             "g2": {"project_id": "shop-api", "goal": "Add orders", "created_at": "20"},
             "g3": {"project_id": "elsewhere", "goal": "x", "created_at": "30"}}
    events = activity_routes.group_timeline(["shop", "shop-api"], {"shop": "Shop", "shop-api": "Shop API"}, goals, {},
                                            {"shop": [json.dumps({"at": 15, "kind": "deploy", "title": "App deployed"})]})
    assert [(e["project_name"], e["title"]) for e in events] == [
        ("Shop API", "Goal started: Add orders"), ("Shop", "App deployed"), ("Shop", "Goal started: Add cart")]
