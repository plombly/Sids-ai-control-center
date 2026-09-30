import json

import pytest
from fastapi.testclient import TestClient

import main


class FakeRedis:
    def __init__(self, hashes=None, members=()):
        self.hashes = hashes or {}
        self.members = set(members)
        self.strings = {}
        self.queues = []

    def smembers(self, key):
        return self.members if key == "sid:projects" else set()

    def scan_iter(self, pattern):
        prefix = pattern[:-1]
        return iter(sorted(key for key in self.hashes if key.startswith(prefix)))

    def hgetall(self, key):
        return self.hashes.get(key, {}) or {}

    def hget(self, key, field):
        return self.hgetall(key).get(field)

    def hset(self, key, key_or_value=None, value=None, mapping=None, **kwargs):
        data = self.hashes.setdefault(key, {})
        if mapping is not None:
            data.update(mapping)
        elif isinstance(key_or_value, dict):
            data.update(key_or_value)
        else:
            data[key_or_value] = value
        return 1

    def get(self, key):
        return self.strings.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    def rpush(self, key, value):
        self.queues.append((key, value))


@pytest.fixture
def client(monkeypatch):
    fake = FakeRedis({
        "sid:projects:alpha": {"id": "alpha", "name": "Alpha", "importance": "high", "status": "active", "created_at": "4", "deploy_key": "secret"},
        "sid:projects:old": {"id": "old", "name": "Old", "importance": "low", "status": "archived"},
        "sid:project-stats:alpha": {"remaining_effort": "3.5", "waiting_jobs": "bad", "running_jobs": "2"},
        "sid:goals:g1": {"id": "g1", "project_id": "alpha", "status": "running", "updated_at": "20"},
        "sid:goals:g2": {"id": "g2", "status": "queued", "updated_at": "10"},
        "sid:goals:g3:planning": {"project_id": "alpha", "status": "running"},
        "sid:jobs:j1": {"id": "j1", "project_id": "alpha", "role": "builder", "status": "running", "updated_at": "30"},
        "sid:jobs:j2": {"id": "j2", "project_id": "alpha", "role": "reviewer", "status": "merged", "updated_at": "40"},
        "sid:jobs:j3": {"id": "j3", "status": "queued", "updated_at": "5"},
    }, members={"alpha", "old"})
    monkeypatch.setattr(main, "redis", fake)
    with TestClient(main.app) as test_client:
        yield test_client, fake


def test_projects_order_filter_and_counts(client):
    test_client, _ = client
    response = test_client.get("/api/projects")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["alpha", "sid"]
    assert test_client.get("/api/projects?include_archived=true").json()[-1]["id"] == "old"
    alpha = response.json()[0]
    assert alpha["counts"] == {"goals_active": 1, "jobs_queued": 0, "jobs_running": 1, "jobs_awaiting_approval": 0, "jobs_needs_human": 0, "jobs_merged": 0}
    assert alpha["stats"] == {"remaining_effort": 3.5, "waiting_jobs": None, "running_jobs": 2}


def test_detail_submission_and_patch(client):
    test_client, fake = client
    detail = test_client.get("/api/projects/alpha?limit=1").json()
    assert [item["id"] for item in detail["goals"]] == ["g1"]
    assert [item["id"] for item in detail["jobs"]] == ["j1"]
    response = test_client.post("/api/projects/alpha/goals", json={"goal": "ship it"})
    assert response.status_code == 202 and response.json()["project_id"] == "alpha"
    goal_id = response.json()["id"]
    assert fake.hashes[f"sid:goals:{goal_id}"]["project_id"] == "alpha"
    assert json.loads(fake.queues[-1][1])["project_id"] == "alpha"
    assert test_client.patch("/api/projects/alpha", json={"importance": "low"}).json()["importance"] == "low"
    assert test_client.patch("/api/projects/sid", json={"importance": "high"}).status_code == 200


def test_project_validation_and_protected_fields(client):
    test_client, _ = client
    assert test_client.get("/api/projects/Bad!").status_code == 422
    assert test_client.get("/api/projects/missing").status_code == 404
    assert test_client.get("/api/projects/alpha?limit=0").status_code == 422
    assert test_client.post("/api/projects/alpha/goals", json={"goal": ""}).status_code == 422
    assert test_client.post("/api/projects/old/goals", json={"goal": "x"}).status_code == 409
    assert test_client.get("/api/projects/alpha").status_code == 200
    assert test_client.patch("/api/projects/alpha", json={"importance": "urgent"}).status_code == 422
    assert "deploy_key" not in test_client.get("/api/projects/alpha").text
