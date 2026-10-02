"""Builder-managed projects (LAIka itself and its app) are view-only."""

import pytest
from fastapi.testclient import TestClient

import main
import managed
from test_device_routes import DeviceRedis


@pytest.fixture
def api(monkeypatch):
    fake = DeviceRedis({
        "sid:projects:app": {"id": "app", "name": "App", "status": "active", "view_only": "1"},
        "sid:projects:shop": {"id": "shop", "name": "Shop", "status": "active"},
        "sid:jobs:j-app": {"id": "j-app", "status": "needs_human", "project_id": "app"},
        "sid:jobs:j-sid": {"id": "j-sid", "status": "awaiting_review"},
        "sid:assist:" + "a" * 16: {"project_id": "app", "status": "brief"},
    }, members=["app", "shop"])
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(main, "OPERATOR_TOKEN", "")
    return TestClient(main.app), fake


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/projects/app/goals", {"goal": "x"}),
    ("post", "/api/projects/app/assistant", {"idea": "x"}),
    ("patch", "/api/projects/app", {"importance": "low"}),
    ("post", "/api/projects/app/builds", None),
    ("post", "/api/projects/app/recheck", None),
    ("put", "/api/projects/app/env", {"name": "A", "value": "b"}),
    ("post", "/api/projects/app/files/data/op", {"op": "mkdir", "path": "x"}),
    ("post", "/api/projects/app/undo", {"commit": "abc1234", "request_id": "undo-00001"}),
    ("post", "/api/projects/app/delete", {"confirm": "app", "request_id": "req-del-00001"}),
    ("post", "/api/jobs/j-app/actions", {"action": "reject", "request_id": "req-00000001", "expected_status": "needs_human"}),
    ("post", "/api/jobs/j-sid/actions", {"action": "reject", "request_id": "req-00000002", "expected_status": "awaiting_review"}),
    ("post", "/api/jobs/j-app/preview", None),
    ("post", "/api/assistant/" + "a" * 16 + "/reply", {"feedback": "x"}),
    ("post", "/api/goals", {"goal": "x"}),
    ("patch", "/api/projects/sid", {"importance": "low"}),
])
def test_every_write_to_a_managed_project_is_refused(api, method, path, body):
    client, _ = api
    kwargs = {"json": body} if body is not None else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 403 and response.json()["detail"] == managed.MESSAGE


def test_reads_stay_open_and_other_projects_are_untouched(api):
    client, _ = api
    assert client.get("/api/projects/app").json()["view_only"] is True
    assert client.get("/api/projects/sid").json()["view_only"] is True
    assert client.get("/api/projects/shop").json()["view_only"] is False
    assert client.patch("/api/projects/shop", json={"importance": "low"}).status_code == 200
    # Unknown jobs get the endpoint's own answer, not "view-only".
    assert client.post("/api/jobs/nope/actions", json={"action": "reject", "request_id": "req-00000003",
                                                        "expected_status": "x"}).status_code != 403


def test_production_installs_have_no_built_in_project(api, monkeypatch):
    import project_routes
    client, _ = api
    monkeypatch.setattr(project_routes, "BUILTIN_PROJECT", False)
    assert "sid" not in {p["id"] for p in client.get("/api/projects").json()}
    assert client.get("/api/projects/sid").status_code == 404
    assert "sid" not in {p["id"] for p in client.get("/api/app/summary").json()["projects"]}
    monkeypatch.setattr(project_routes, "BUILTIN_PROJECT", True)
    assert "sid" in {p["id"] for p in client.get("/api/projects").json()}
