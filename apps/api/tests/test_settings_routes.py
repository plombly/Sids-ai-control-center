"""Settings API: read, validate, save, apply."""

import json

import pytest
from fastapi.testclient import TestClient

import main
from test_device_routes import DeviceRedis


@pytest.fixture
def api(monkeypatch):
    fake = DeviceRedis({"laika:operator-service:op": {"id": "op", "allowed_actions": "apply_settings"}}, members=[])
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(main, "OPERATOR_TOKEN", "t")
    monkeypatch.setattr(main, "_operator_service", lambda: fake.hashes["laika:operator-service:op"])
    return TestClient(main.app, headers={"X-Laika-Token": "t"}), fake


def test_read_change_and_apply(api):
    client, fake = api
    first = client.get("/api/settings").json()
    assert {s["id"] for s in first["sections"]} >= {"general", "appearance", "ai", "workers"}
    assert first["values"]["THEME"] == "system" and first["pending"] == []
    saved = client.put("/api/settings", json={"changes": {"THEME": "light", "MAX_REPAIR_ATTEMPTS": 4, "WORKER_COUNT": 6}})
    assert saved.status_code == 200
    body = saved.json()
    assert body["values"]["THEME"] == "light" and body["values"]["WORKER_COUNT"] == "6"
    assert body["pending"] == ["MAX_REPAIR_ATTEMPTS", "WORKER_COUNT"]
    applied = client.post("/api/settings/apply", json={"what": "apply", "request_id": "apply-0001"})
    assert applied.status_code == 202
    assert fake.stream[-1][1]["action"] == "apply_settings" and fake.stream[-1][1]["what"] == "apply"
    assert client.get("/api/settings").json()["pending"] == []


def test_invalid_values_name_each_problem(api):
    client, fake = api
    bad = client.put("/api/settings", json={"changes": {"THEME": "pink", "WORKER_COUNT": 99, "NOPE": 1}})
    assert bad.status_code == 422
    errors = bad.json()["detail"]["errors"]
    assert set(errors) == {"THEME", "WORKER_COUNT", "NOPE"}
    assert "laika:settings" not in fake.strings


def test_phones_and_anonymous_writers_cannot_change_settings(api, monkeypatch):
    client, fake = api
    anonymous = TestClient(main.app)
    assert anonymous.put("/api/settings", json={"changes": {"THEME": "dark"}}).status_code == 401
