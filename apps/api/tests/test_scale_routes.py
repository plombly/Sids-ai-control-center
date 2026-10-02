"""Worker scaling from the dashboard: state, + / -, limits, fixed mode."""

import json

import pytest
from fastapi.testclient import TestClient

import main
from test_device_routes import DeviceRedis


class ScaleRedis(DeviceRedis):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.lists = {}

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start:end + 1]

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start:end + 1]


@pytest.fixture
def api(monkeypatch):
    fake = ScaleRedis({}, members=[])
    monkeypatch.setattr(main, "redis", fake)
    return TestClient(main.app), fake


def state(fake, **fields):
    fake.strings["laika:scaler:state"] = json.dumps({"target": 3, "auto": True, "min": 1, "max": 4, "fixed": 8, **fields})
    fake.strings["laika:scaler:target"] = str(fields.get("target", 3))


def test_needs_the_scaler(api):
    client, fake = api
    assert client.post("/api/workers/scale", json={"delta": 1}).status_code == 503
    assert client.get("/api/workers/scale").json()["state"] is None


def test_automatic_mode_moves_the_target_and_pauses_automatic_decisions(api):
    client, fake = api
    state(fake)
    assert client.post("/api/workers/scale", json={"delta": 1}).json() == {"target": 4, "auto": True}
    assert fake.strings["laika:scaler:target"] == "4" and float(fake.strings["laika:scaler:manual-until"]) > 0
    refused = client.post("/api/workers/scale", json={"delta": 1})
    assert refused.status_code == 409 and "most" in refused.json()["detail"]
    assert client.post("/api/workers/scale", json={"delta": 2}).status_code == 422
    log = client.get("/api/workers/scale").json()["log"]
    assert log[0]["reason"] == "added by hand" and log[0]["target"] == 4


def test_fixed_mode_changes_the_setting(api):
    client, fake = api
    state(fake, auto=False, fixed=5, target=5)
    assert client.post("/api/workers/scale", json={"delta": -1}).json()["target"] == 4
    assert json.loads(fake.strings["laika:settings"])["WORKER_COUNT"] == "4"


def test_system_update_state_and_request(api):
    client, fake = api
    fake.strings["laika:system-info"] = json.dumps({"version": "1.0.0"})
    assert client.get("/api/system/update").json() == {"version": "1.0.0", "available": None, "status": None}
    assert client.post("/api/system/update", json={"request_id": "upd-00000001"}).status_code == 409
    fake.strings["laika:update:available"] = json.dumps({"newer": True, "latest": "1.0.1"})
    fake.strings["laika:update:status"] = json.dumps({"state": "installing"})
    assert client.post("/api/system/update", json={"request_id": "upd-00000002"}).status_code == 409
    fake.strings["laika:update:status"] = json.dumps({"state": "done"})
    fake.hashes["laika:operator-service:op"] = {"id": "op", "allowed_actions": "system_update"}
    import main
    original = main._operator_service
    main._operator_service = lambda: fake.hashes["laika:operator-service:op"]
    try:
        accepted = client.post("/api/system/update", json={"request_id": "upd-00000003"})
    finally:
        main._operator_service = original
    assert accepted.status_code == 202 and fake.stream[-1][1]["action"] == "system_update"
