"""Phones: per-device keys, what they may do, and the app API."""

import json

import pytest
from fastapi.testclient import TestClient

import device_routes
import main
from test_project_routes import FakeRedis


class DeviceRedis(FakeRedis):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.sets = {}

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    def smembers(self, key):
        return self.members if key == "sid:projects" else set(self.sets.get(key, set()))

    def delete(self, key):
        self.strings.pop(key, None)
        self.hashes.pop(key, None)

    def expire(self, key, seconds):
        return True

    def hsetnx(self, key, field, value):
        if field in self.hashes.get(key, {}):
            return False
        self.hashes.setdefault(key, {})[field] = value
        return True

    def xadd(self, *a, **k):
        return "1-0"


@pytest.fixture
def api(monkeypatch):
    fake = DeviceRedis({"sid:projects:shop": {"id": "shop", "name": "Shop", "status": "active"},
                        "sid:jobs:j1": {"id": "j1", "status": "needs_human", "needs_human_kind": "network", "project_id": "shop"},
                        "sid:operator-service:op": {"id": "op", "allowed_actions": "approve,reject,network_once"}},
                       members=["shop"])
    fake.strings["sid:system-info"] = json.dumps({"head": "abcdef1234567890"})
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(main, "OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setattr(main, "_submit_goal", lambda payload, project_id="sid": {"id": "g1", "status": "accepted"})
    monkeypatch.setattr(main, "_operator_service", lambda: fake.hashes["sid:operator-service:op"])
    return TestClient(main.app), fake


OPERATOR = {"X-SID-Token": "operator-secret"}


def add_phone(client):
    response = client.post("/api/devices", headers=OPERATOR,
                           json={"name": "Dylan's phone", "url": "http://10.0.0.59:8000", "server_name": "Home SID"})
    assert response.status_code == 201
    return response.json()


def test_adding_a_phone_shows_its_key_once_and_stores_only_a_hash(api):
    client, fake = api
    created = add_phone(client)
    key = created["key"]
    assert key.startswith("sidk_") and created["pairing"] == {"v": 1, "name": "Home SID", "url": "http://10.0.0.59:8000", "key": key}
    assert json.loads(created["pairing_text"])["key"] == key
    assert created["qr_svg"].startswith("<svg")
    stored = fake.hashes[f"sid:devices:{created['device']['id']}"]
    assert key not in json.dumps(stored) and stored["key_hash"] == device_routes.key_hash(key)
    listed = client.get("/api/devices").json()
    assert [d["name"] for d in listed["devices"]] == ["Dylan's phone"] and "key" not in json.dumps(listed)
    # Adding devices needs the operator token.
    assert client.post("/api/devices", json={"name": "x", "url": "http://a:1"}).status_code == 401


def test_a_device_does_safe_writes_only(api):
    client, fake = api
    key = add_phone(client)["key"]
    phone = {"Authorization": f"Bearer {key}"}
    assert client.post("/api/projects/shop/goals", headers=phone, json={"goal": "x"}).status_code == 202
    answer = client.post("/api/jobs/j1/actions", headers=phone,
                         json={"action": "network_once", "request_id": "req-12345678", "expected_status": "needs_human"})
    assert answer.status_code == 202 and answer.json()["requested_from"].startswith("device Dylan's phone")
    approve = client.post("/api/jobs/j1/actions", headers=phone,
                          json={"action": "approve", "request_id": "req-87654321", "expected_status": "needs_human",
                                "expected_candidate": "a" * 40})
    assert approve.status_code == 403 and "dashboard" in approve.json()["detail"]
    for path, body in (("/api/projects/shop", None), ("/api/devices", {"name": "x", "url": "http://a:1"}),
                       ("/api/notifications/test", {})):
        method = client.patch if path == "/api/projects/shop" else client.post
        assert method(path, headers=phone, json=body or {"importance": "low"}).status_code == 403, path
    assert client.delete("/api/devices/aaaaaaaaaaaa", headers=phone).status_code == 403


def test_revoked_or_unknown_keys_get_nothing(api):
    client, fake = api
    created = add_phone(client)
    phone = {"Authorization": f"Bearer {created['key']}"}
    assert client.get("/api/app/info", headers=phone).json()["device"]["name"] == "Dylan's phone"
    assert client.delete(f"/api/devices/{created['device']['id']}", headers=OPERATOR).status_code == 200
    assert client.post("/api/projects/shop/goals", headers=phone, json={"goal": "x"}).status_code == 401
    assert client.get("/api/app/info", headers=phone).json()["device"] is None
    assert client.get("/api/devices").json()["devices"] == []
    wrong = {"Authorization": "Bearer sidk_made-up"}
    assert client.post("/api/projects/shop/goals", headers=wrong, json={"goal": "x"}).status_code == 401


def test_app_info_and_summary(api):
    client, fake = api
    info = client.get("/api/app/info").json()
    assert info["api_version"] == 1 and info["sid_commit"] == "abcdef123456" and info["device"] is None
    assert "approve" not in info["device_actions"]
    summary = client.get("/api/app/summary").json()
    assert [j["id"] for j in summary["needs_you"]["stuck"]] == ["j1"]
    assert summary["needs_you"]["stuck"][0]["needs_human_kind"] == "network"
    assert {p["id"] for p in summary["projects"]} >= {"shop", "sid"}
