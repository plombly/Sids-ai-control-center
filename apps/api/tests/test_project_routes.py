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
        return self.members if key == "laika:projects" else set()

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
        "laika:projects:alpha": {"id": "alpha", "name": "Alpha", "importance": "high", "status": "active", "created_at": "4", "deploy_key": "secret"},
        "laika:projects:old": {"id": "old", "name": "Old", "importance": "low", "status": "archived"},
        "laika:project-stats:alpha": {"remaining_effort": "3.5", "waiting_jobs": "bad", "running_jobs": "2"},
        "laika:goals:g1": {"id": "g1", "project_id": "alpha", "status": "running", "updated_at": "20"},
        "laika:goals:g2": {"id": "g2", "status": "queued", "updated_at": "10"},
        "laika:goals:g3:planning": {"project_id": "alpha", "status": "running"},
        "laika:jobs:j1": {"id": "j1", "project_id": "alpha", "role": "builder", "status": "running", "updated_at": "30"},
        "laika:jobs:j2": {"id": "j2", "project_id": "alpha", "role": "reviewer", "status": "merged", "updated_at": "40"},
        "laika:jobs:j3": {"id": "j3", "status": "queued", "updated_at": "5"},
    }, members={"alpha", "old"})
    monkeypatch.setattr(main, "redis", fake)
    with TestClient(main.app) as test_client:
        yield test_client, fake


def test_projects_order_filter_and_counts(client):
    test_client, _ = client
    response = test_client.get("/api/projects")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["alpha", "laika"]
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
    assert fake.hashes[f"laika:goals:{goal_id}"]["project_id"] == "alpha"
    assert json.loads(fake.queues[-1][1])["project_id"] == "alpha"
    assert test_client.patch("/api/projects/alpha", json={"importance": "low"}).json()["importance"] == "low"
    assert test_client.patch("/api/projects/laika", json={"importance": "high"}).status_code == 403  # view-only


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
    fake.hashes["laika:operator-service:op"] = {"id": "op", "allowed_actions": allowed}
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
    assert stream == "laika:operator-requests"
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
    assert _create(test_client, id="laika").status_code == 409
    del fake.hashes["laika:operator-service:op"]
    assert _create(test_client).status_code == 503
    _operator_ready(fake, allowed="reject")
    assert _create(test_client).status_code == 403


def test_retry_clone_only_for_projects_waiting_for_a_key(client):
    test_client, fake = client
    _operator_ready(fake)
    fake.hashes["laika:projects:cloned"] = {"id": "cloned", "name": "C", "status": "pending_key", "importance": "low"}
    fake.members.add("cloned")
    ok = test_client.post("/api/projects/cloned/retry-clone", json={"request_id": "req-retry-001"})
    assert ok.status_code == 202 and fake.stream[-1][1]["action"] == "project_retry_clone"
    fake.hashes["laika:projects:cloned"]["status"] = "active"
    assert test_client.post("/api/projects/cloned/retry-clone", json={"request_id": "req-retry-002"}).status_code == 409


def test_push_setup_request(client):
    test_client, fake = client
    _operator_ready(fake)
    refused = test_client.post("/api/projects/laika/push-setup",
                               json={"url": "git@github.com:me/laika.git", "request_id": "req-push-0000"})
    assert refused.status_code == 403 and fake.stream == []
    response = test_client.post("/api/projects/alpha/push-setup",
                                json={"url": "git@github.com:me/laika.git", "request_id": "req-push-0001"})
    assert response.status_code == 202
    assert fake.stream[-1][1] | {} == {**fake.stream[-1][1], "action": "project_push_setup", "url": "git@github.com:me/laika.git"}
    assert test_client.post("/api/projects/ghost/push-setup",
                            json={"url": "git@github.com:me/x.git", "request_id": "req-push-0002"}).status_code == 404


def test_same_prompt_in_two_projects_is_two_goals(client):
    test_client, fake = client
    fake.hashes["laika:projects:other"] = {"id": "other", "name": "O", "status": "active", "importance": "low"}
    fake.members.add("other")
    fake.hashes["laika:projects:alpha"]["status"] = "active"
    first = test_client.post("/api/projects/alpha/goals", json={"goal": "add a readme"}).json()
    second = test_client.post("/api/projects/other/goals", json={"goal": "add a readme"}).json()
    assert first["id"] != second["id"] and not second.get("duplicate")
    again = test_client.post("/api/projects/other/goals", json={"goal": "add a readme"}).json()
    assert again["id"] == second["id"] and again.get("duplicate")


def test_delete_project_needs_the_typed_id_and_never_laika(client):
    test_client, fake = client
    _operator_ready(fake)
    url = "/api/projects/alpha/delete"
    assert test_client.post(url, json={"confirm": "alph", "request_id": "req-del-0001"}).status_code == 422
    assert test_client.post("/api/projects/laika/delete", json={"confirm": "laika", "request_id": "req-del-0002"}).status_code == 403
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
    fake.hashes["laika:projects:old"]["run_port"] = "8106"
    assert test_client.patch("/api/projects/alpha", json={"run_port": 8106}).status_code == 409
    # The built-in project is never editable from the web.
    assert test_client.patch("/api/projects/laika", json={"gate_command": "true"}).status_code == 403
    assert "laika:projects:laika" not in fake.hashes


def test_app_status_and_restart(client):
    test_client, fake = client
    fake.hashes["laika:app-status:alpha"] = {"state": "running", "port": "8100", "commit": "abc", "log": ""}
    assert test_client.get("/api/projects/alpha").json()["app"]["state"] == "running"
    assert test_client.post("/api/projects/alpha/app/restart").status_code == 202
    assert "restart_at" in fake.hashes["laika:projects:alpha"]
    assert test_client.post("/api/projects/laika/app/restart").status_code == 403


def test_laika_project_shows_its_system_info_and_real_gate(client):
    test_client, fake = client
    fake.strings["laika:system-info"] = json.dumps({"remote": "git@github.com:me/laika.git", "branch": "main", "head": "abc1234"})
    laika = test_client.get("/api/projects/laika").json()
    assert laika["system"]["remote"] == "git@github.com:me/laika.git" and laika["gate_command"] == "scripts/integration-check.py"
    assert test_client.get("/api/projects/alpha").json()["system"] is None


def test_trash_listing_and_restore_request(client):
    test_client, fake = client
    fake.smembers = lambda key: {"shop-20260930T220000Z"} if key == "laika:trash" else (fake.members if key == "laika:projects" else set())
    fake.hashes["laika:trash:shop-20260930T220000Z"] = {"trash_id": "shop-20260930T220000Z", "project_id": "shop",
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


def test_history_and_undo(client):
    test_client, fake = client
    fake.strings["laika:history:alpha"] = json.dumps({"head": "h1", "changes": [{"kind": "job", "job_id": "j1", "title": "T"}]})
    body = test_client.get("/api/projects/alpha/history").json()
    assert body["changes"][0]["job_id"] == "j1" and body["can_undo"] is True
    assert test_client.get("/api/projects/laika/history").json()["can_undo"] is False
    _operator_ready(fake, allowed="project_revert")
    ok = test_client.post("/api/projects/alpha/undo", json={"job": "j1", "request_id": "undo-00001"})
    assert ok.status_code == 202 and fake.stream[-1][1]["undo_job"] == "j1"
    assert test_client.post("/api/projects/alpha/undo", json={"request_id": "undo-00002"}).status_code == 422
    assert test_client.post("/api/projects/laika/undo", json={"commit": "abc1234", "request_id": "undo-00003"}).status_code == 403
    assert test_client.post("/api/projects/alpha/undo", json={"commit": "--hard", "request_id": "undo-00004"}).status_code == 422


# --- project types and builds ------------------------------------------------------

class BuildRedis(FakeRedis):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.lists = {}

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start:end + 1]

    def delete(self, key):
        self.strings.pop(key, None)


@pytest.fixture
def builds(monkeypatch, tmp_path):
    import build_routes
    fake = BuildRedis({"laika:projects:game": {"id": "game", "name": "Game", "status": "active"}}, members=["game"])
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(build_routes, "PROJECTS_MOUNT", tmp_path)
    return TestClient(main.app), fake, tmp_path


def test_type_comes_from_choice_detection_or_description(builds):
    client, fake, _ = builds
    assert client.get("/api/projects/game").json()["project_type"]["type"] == "checking"
    fake.strings["laika:project-type:game"] = json.dumps({"type": "unknown", "stack": "", "evidence": []})
    assert client.get("/api/projects/game").json()["project_type"]["type"] == "unknown"
    assert client.patch("/api/projects/game", json={"type_description": "a discord bot for my server"}).status_code == 200
    kind = client.get("/api/projects/game").json()["project_type"]
    assert (kind["type"], kind["source"]) == ("bot", "described")
    fake.strings["laika:project-type:game"] = json.dumps({"type": "game", "stack": "love2d", "evidence": ["main.lua"]})
    kind = client.get("/api/projects/game").json()["project_type"]
    assert (kind["type"], kind["stack"], kind["evidence"]) == ("game", "love2d", ["main.lua"])
    assert client.patch("/api/projects/game", json={"type": "desktop_app"}).json()["project_type"]["source"] == "chosen"
    assert client.patch("/api/projects/game", json={"type": "spaceship"}).status_code == 422
    assert client.post("/api/projects/game/recheck").status_code == 202
    assert "laika:project-type:game" not in fake.strings
    catalog = client.get("/api/project-catalog").json()
    assert "game" in catalog["types"] and "keywords" not in catalog["types"]["game"]
    assert catalog["types"]["game"]["templates"]


def test_build_fields_are_validated(builds):
    client, fake, _ = builds
    for body in ({"build_image": "--privileged"}, {"build_image": "a b"}, {"build_output": "../x"},
                 {"build_output": "/etc"}, {"build_command": "a\nb"}):
        assert client.patch("/api/projects/game", json=body).status_code == 422, body
    ok = client.patch("/api/projects/game", json={"build_image": "node:22", "build_command": "npm run dist", "build_output": "out"})
    assert ok.status_code == 200 and ok.json()["build_image"] == "node:22"
    # The built-in project takes nothing from the web.
    assert client.patch("/api/projects/laika", json={"build_command": "rm -rf /"}).status_code == 403


def test_build_request_list_download_and_log(builds):
    client, fake, folder = builds
    assert client.post("/api/projects/game/builds").status_code == 409  # nothing detected: no recipe
    fake.strings["laika:project-type:game"] = json.dumps({"type": "game", "stack": "unity"})
    refused = client.post("/api/projects/game/builds")
    assert refused.status_code == 409 and "Unity" in refused.json()["detail"]
    fake.strings["laika:project-type:game"] = json.dumps({"type": "game", "stack": "love2d"})
    first = client.post("/api/projects/game/builds")
    assert first.status_code == 202
    assert client.post("/api/projects/game/builds").status_code == 409  # already requested
    request = json.loads(fake.strings["laika:build-request:game"])
    assert request["build_id"] == first.json()["build_id"]
    listing = client.get("/api/projects/game/builds").json()
    assert listing["requested"] and listing["recipe"]["label"].startswith("LÖVE")
    fake.strings.pop("laika:build-request:game")
    fake.lists["laika:builds:game"] = ["b2", "b1"]
    fake.hashes["laika:build:game:b2"] = {"status": "running"}
    fake.hashes["laika:build:game:b1"] = {"status": "succeeded", "commit": "abcdef1234567", "size": "3"}
    assert client.post("/api/projects/game/builds").status_code == 409  # one at a time
    items = client.get("/api/projects/game/builds").json()["builds"]
    assert [(b["id"], b["status"]) for b in items] == [("b2", "running"), ("b1", "succeeded")]
    (folder / "game/builds").mkdir(parents=True)
    (folder / "game/builds/b1.zip").write_bytes(b"PK")
    (folder / "game/builds/b1.log").write_text("built ok\n")
    got = client.get("/api/projects/game/builds/b1/download")
    assert got.status_code == 200 and got.content == b"PK" and "game-b1-abcdef12.zip" in got.headers["content-disposition"]
    assert client.get("/api/projects/game/builds/b1/log").text == "built ok\n"
    assert client.get("/api/projects/game/builds/b2/download").status_code == 404  # not finished
    assert client.get("/api/projects/game/builds/..%2Fx/log").status_code == 404
    assert client.get("/api/projects/laika/builds").status_code == 404


def test_internet_access_settings(builds):
    client, fake, _ = builds
    item = client.get("/api/projects/game").json()
    assert (item["gate_network"], item["build_network"]) == ("", "internet")
    changed = client.patch("/api/projects/game", json={"gate_network": "always", "build_network": "none"}).json()
    assert (changed["gate_network"], changed["build_network"]) == ("always", "none")
    assert client.patch("/api/projects/game", json={"gate_network": "sometimes"}).status_code == 422
    assert client.patch("/api/projects/laika", json={"gate_network": "always"}).status_code == 403


class GroupRedis(BuildRedis):
    def hdel(self, key, *fields):
        for field in fields:
            self.hashes.get(key, {}).pop(field, None)

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start:end + 1]


@pytest.fixture
def groups(monkeypatch):
    fake = GroupRedis({
        "laika:projects:shop": {"id": "shop", "name": "Shop", "status": "active", "importance": "high", "gate_network": "always"},
        "laika:projects:app": {"id": "app", "name": "App", "status": "active", "importance": "low"},
        "laika:projects:blog": {"id": "blog", "name": "Blog", "status": "active"},
    }, members=["shop", "app", "blog"])
    monkeypatch.setattr(main, "redis", fake)
    return TestClient(main.app), fake


def test_attach_inherit_and_detach(groups):
    client, fake = groups
    joined = client.post("/api/projects/app/parent", json={"parent": "shop"})
    assert joined.status_code == 200
    item = joined.json()
    assert (item["parent"], item["parent_name"], item["importance"], item["gate_network"]) == ("shop", "Shop", "high", "always")
    assert item["managed"] == ["importance", "gate_network"]
    parent = client.get("/api/projects/shop").json()
    assert parent["children"] == [{"id": "app", "name": "App", "status": "active"}]
    # Inherited settings cannot be changed on the child.
    assert client.patch("/api/projects/app", json={"importance": "medium"}).status_code == 409
    assert client.patch("/api/projects/app", json={"run_command": "npm start"}).status_code == 200
    # Archiving the parent archives the group.
    fake.hashes["laika:projects:shop"]["status"] = "archived"
    assert client.get("/api/projects/app").json()["status"] == "archived"
    fake.hashes["laika:projects:shop"]["status"] = "active"
    assert "joined" in json.loads(fake.lists["laika:events:app"][0])["title"].lower()
    left = client.post("/api/projects/app/parent", json={"parent": ""}).json()
    assert left["parent"] == "" and left["importance"] == "low"


@pytest.mark.parametrize("child,parent,status", [
    ("app", "laika", 200), ("laika", "app", 403), ("app", "app", 409), ("app", "nope", 409), ("app", "Bad!", 422), ("nope", "shop", 404),
])
def test_attach_rules(groups, child, parent, status):
    client, _ = groups
    assert client.post(f"/api/projects/{child}/parent", json={"parent": parent}).status_code == status


def test_groups_have_one_level(groups):
    client, _ = groups
    assert client.post("/api/projects/app/parent", json={"parent": "shop"}).status_code == 200
    assert client.post("/api/projects/blog/parent", json={"parent": "app"}).status_code == 409  # app is a child
    assert client.post("/api/projects/shop/parent", json={"parent": "blog"}).status_code == 409  # shop has children


def test_build_all_builds_every_buildable_member_of_the_group(builds):
    client, fake, _ = builds
    fake.hashes["laika:projects:game-server"] = {"id": "game-server", "name": "Server", "status": "active", "parent": "game"}
    fake.hashes["laika:projects:game-app"] = {"id": "game-app", "name": "App", "status": "active", "parent": "game",
                                              "view_only": "1"}
    fake.members |= {"game-server", "game-app"}
    assert client.post("/api/projects/game/builds/all").status_code == 409  # nothing buildable yet
    fake.strings["laika:project-type:game"] = json.dumps({"type": "game", "stack": "love2d"})
    fake.strings["laika:project-type:game-server"] = json.dumps({"type": "game", "stack": "unity"})
    result = client.post("/api/projects/game-server/builds/all").json()   # from any member
    assert result["group"] == "game" and [b["id"] for b in result["started"]] == ["game"]
    reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert "Unity" in reasons["game-server"] and "managed by the LAIka builder" in reasons["game-app"]
