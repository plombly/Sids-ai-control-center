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


# --- host-side project management through the operator service ----------------------

def _operator_ready(fake, allowed="create_project,project_retry_clone,project_push_setup,delete_project"):
    fake.hashes["sid:operator-service:op"] = {"id": "op", "allowed_actions": allowed}
    fake.stream = []

    def hsetnx(key, field, value):
        data = fake.hashes.setdefault(key, {})
        if field in data:
            return 0
        data[field] = value
        return 1

    fake.hsetnx = hsetnx
    fake.xadd = lambda name, fields, maxlen=None, approximate=True: fake.stream.append((name, dict(fields))) or "1-0"


def _create(test_client, **fields):
    body = {"id": "new-app", "name": "New App", "importance": "high", "source": "empty",
            "request_id": "req-create-01", **fields}
    return test_client.post("/api/projects", json=body)


def test_create_project_queues_a_host_request(client):
    test_client, fake = client
    _operator_ready(fake)
    response = _create(test_client)
    assert response.status_code == 202 and response.json()["status"] == "pending"
    [(stream, fields)] = fake.stream
    assert stream == "sid:operator-requests"
    assert (fields["action"], fields["project_id"], fields["source"], fields["importance"]) == (
        "create_project", "new-app", "empty", "high")
    assert fields["url"] == "" and fields["job_id"] == ""


def test_create_project_clone_needs_a_network_git_url(client):
    test_client, fake = client
    _operator_ready(fake)
    assert _create(test_client, source="clone").status_code == 422
    assert _create(test_client, source="clone", url="/etc", request_id="req-create-02").status_code == 422
    ok = _create(test_client, source="clone", url="git@github.com:me/app.git", request_id="req-create-03")
    assert ok.status_code == 202 and fake.stream[-1][1]["url"] == "git@github.com:me/app.git"


@pytest.mark.parametrize("fields", [
    {"id": "Bad_Id"}, {"name": ""}, {"importance": "urgent"}, {"gate": "a\nb"},
    {"push_remote": "file:///x"}, {"request_id": "x"},
])
def test_create_project_validation(client, fields):
    test_client, fake = client
    _operator_ready(fake)
    assert _create(test_client, **fields).status_code == 422
    assert fake.stream == []


def test_create_project_refuses_existing_ids_and_needs_the_operator(client):
    test_client, fake = client
    _operator_ready(fake)
    assert _create(test_client, id="sid").status_code == 409
    del fake.hashes["sid:operator-service:op"]
    assert _create(test_client).status_code == 503
    _operator_ready(fake, allowed="reject")
    assert _create(test_client).status_code == 403


def test_retry_clone_only_for_projects_waiting_for_a_key(client):
    test_client, fake = client
    _operator_ready(fake)
    fake.hashes["sid:projects:cloned"] = {"id": "cloned", "name": "C", "status": "pending_key", "importance": "low"}
    fake.members.add("cloned")
    ok = test_client.post("/api/projects/cloned/retry-clone", json={"request_id": "req-retry-001"})
    assert ok.status_code == 202 and fake.stream[-1][1]["action"] == "project_retry_clone"
    fake.hashes["sid:projects:cloned"]["status"] = "active"
    assert test_client.post("/api/projects/cloned/retry-clone", json={"request_id": "req-retry-002"}).status_code == 409


def test_push_setup_request(client):
    test_client, fake = client
    _operator_ready(fake)
    refused = test_client.post("/api/projects/sid/push-setup",
                               json={"url": "git@github.com:me/sid.git", "request_id": "req-push-0000"})
    assert refused.status_code == 403 and fake.stream == []
    response = test_client.post("/api/projects/alpha/push-setup",
                                json={"url": "git@github.com:me/sid.git", "request_id": "req-push-0001"})
    assert response.status_code == 202
    assert fake.stream[-1][1] | {} == {**fake.stream[-1][1], "action": "project_push_setup", "url": "git@github.com:me/sid.git"}
    assert test_client.post("/api/projects/ghost/push-setup",
                            json={"url": "git@github.com:me/x.git", "request_id": "req-push-0002"}).status_code == 404


def test_same_prompt_in_two_projects_is_two_goals(client):
    test_client, fake = client
    fake.hashes["sid:projects:other"] = {"id": "other", "name": "O", "status": "active", "importance": "low"}
    fake.members.add("other")
    first = test_client.post("/api/projects/sid/goals", json={"goal": "add a readme"}).json()
    second = test_client.post("/api/projects/other/goals", json={"goal": "add a readme"}).json()
    assert first["id"] != second["id"] and not second.get("duplicate")
    again = test_client.post("/api/projects/other/goals", json={"goal": "add a readme"}).json()
    assert again["id"] == second["id"] and again.get("duplicate")


def test_delete_project_needs_the_typed_id_and_never_sid(client):
    test_client, fake = client
    _operator_ready(fake)
    url = "/api/projects/alpha/delete"
    assert test_client.post(url, json={"confirm": "alph", "request_id": "req-del-0001"}).status_code == 422
    assert test_client.post("/api/projects/sid/delete", json={"confirm": "sid", "request_id": "req-del-0002"}).status_code == 403
    assert test_client.post("/api/projects/ghost/delete", json={"confirm": "ghost", "request_id": "req-del-0003"}).status_code == 404
    assert fake.stream == []
    ok = test_client.post(url, json={"confirm": "alpha", "request_id": "req-del-0004"})
    assert ok.status_code == 202
    assert fake.stream[-1][1]["action"] == "delete_project" and fake.stream[-1][1]["confirm"] == "alpha"


def test_patch_build_and_run_settings(client):
    test_client, fake = client
    response = test_client.patch("/api/projects/alpha", json={"setup_command": "npm ci", "run_command": "npm start",
                                                               "run_port": 8105})
    assert response.status_code == 200
    body = response.json()
    assert (body["setup_command"], body["run_command"], body["run_port"]) == ("npm ci", "npm start", 8105)
    assert test_client.patch("/api/projects/alpha", json={"run_command": "a\nb"}).status_code == 422
    assert test_client.patch("/api/projects/alpha", json={"run_port": 8080}).status_code == 422
    assert test_client.patch("/api/projects/alpha", json={}).status_code == 422
    fake.hashes["sid:projects:old"]["run_port"] = "8106"
    assert test_client.patch("/api/projects/alpha", json={"run_port": 8106}).status_code == 409
    # SID's own commands are never editable from the web.
    test_client.patch("/api/projects/sid", json={"gate_command": "true", "importance": "high"})
    assert "gate_command" not in fake.hashes["sid:projects:sid"]


def test_app_status_and_restart(client):
    test_client, fake = client
    fake.hashes["sid:app-status:alpha"] = {"state": "running", "port": "8100", "commit": "abc", "log": ""}
    assert test_client.get("/api/projects/alpha").json()["app"]["state"] == "running"
    assert test_client.post("/api/projects/alpha/app/restart").status_code == 202
    assert "restart_at" in fake.hashes["sid:projects:alpha"]
    assert test_client.post("/api/projects/sid/app/restart").status_code == 404


def test_sid_project_shows_its_system_info_and_real_gate(client):
    test_client, fake = client
    fake.strings["sid:system-info"] = json.dumps({"remote": "git@github.com:me/sid.git", "branch": "main", "head": "abc1234"})
    sid = test_client.get("/api/projects/sid").json()
    assert sid["system"]["remote"] == "git@github.com:me/sid.git" and sid["gate_command"] == "scripts/integration-check.py"
    assert test_client.get("/api/projects/alpha").json()["system"] is None


def test_trash_listing_and_restore_request(client):
    test_client, fake = client
    fake.smembers = lambda key: {"shop-20260930T220000Z"} if key == "sid:trash" else (fake.members if key == "sid:projects" else set())
    fake.hashes["sid:trash:shop-20260930T220000Z"] = {"trash_id": "shop-20260930T220000Z", "project_id": "shop",
                                                      "name": "Shop", "deleted_at": "100", "expires_at": "86500"}
    listed = test_client.get("/api/projects-trash").json()
    assert listed == [{"trash_id": "shop-20260930T220000Z", "project_id": "shop", "name": "Shop",
                       "deleted_at": 100, "expires_at": 86500}]
    _operator_ready(fake, allowed="restore_project")
    ok = test_client.post("/api/projects-trash/shop-20260930T220000Z/restore", json={"request_id": "restore-0001"})
    assert ok.status_code == 202 and fake.stream[-1][1]["action"] == "restore_project"
    assert test_client.post("/api/projects-trash/..%2Fx/restore", json={"request_id": "restore-0002"}).status_code in (404, 422)
    assert test_client.post("/api/projects-trash/gone-20260930T220000Z/restore",
                            json={"request_id": "restore-0003"}).status_code == 404
    fake.members.add("shop")
    assert test_client.post("/api/projects-trash/shop-20260930T220000Z/restore",
                            json={"request_id": "restore-0004"}).status_code == 409


def test_app_limits_are_bounded(client):
    test_client, fake = client
    body = test_client.patch("/api/projects/alpha", json={"run_memory_mb": 256, "run_cpus": 0.5, "run_tasks": 64}).json()
    assert (body["run_memory_mb"], body["run_cpus"], body["run_tasks"]) == (256, 0.5, 64)
    assert test_client.get("/api/projects/old?limit=1").status_code in (200, 404)
    for bad in ({"run_memory_mb": 8}, {"run_cpus": 0}, {"run_tasks": 1}):
        assert test_client.patch("/api/projects/alpha", json=bad).status_code == 422
