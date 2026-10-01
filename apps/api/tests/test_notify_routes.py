"""Notification settings API: write-only targets, cleaned settings, preview."""

import pytest
from fastapi.testclient import TestClient

import main
import notify_core
import notify_routes
from test_project_routes import FakeRedis


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(notify_core, "NOTIFY_DIR", tmp_path / "notify")
    monkeypatch.setattr(notify_core, "LEGACY_FILE", tmp_path / "none.env")
    fake = FakeRedis({"sid:projects:shop": {"id": "shop", "name": "Shop", "status": "active"}}, members={"shop"})
    fake.lrange = lambda key, a, b: []
    monkeypatch.setattr(main, "redis", fake)
    with TestClient(main.app) as test_client:
        yield test_client, fake, tmp_path


HOOK = "https://discord.com/api/webhooks/123456/abcDEF_-xyz9"


def test_targets_are_write_only(client):
    test_client, fake, tmp_path = client
    body = test_client.put("/api/notifications/targets", json={"discord_webhook": HOOK, "discord_mention": "269981261508902923"}).json()
    assert body["targets"]["discord"] is True and body["targets"]["discord_hint"] == "…xyz9"
    page = test_client.get("/api/notifications")
    assert HOOK not in page.text and page.json()["targets"]["mention"] == "269981261508902923"
    assert notify_core.load_targets()["DISCORD_WEBHOOK"] == HOOK
    for bad in ({"discord_webhook": "https://evil.example/x"}, {"discord_mention": "@me"}, {"ntfy_url": "ftp://x/y"}):
        assert test_client.put("/api/notifications/targets", json=bad).status_code == 422
    test_client.put("/api/notifications/targets", json={"discord_webhook": ""})
    assert test_client.get("/api/notifications").json()["targets"]["discord"] is False


def test_settings_round_trip_and_test_needs_a_target(client, monkeypatch):
    test_client, fake, tmp_path = client
    saved = test_client.put("/api/notifications/settings", json={"events": {"approval": "ping"}, "quiet": {"enabled": True},
                                                                  "digest": {"day": "fri", "time": "09:30"}}).json()["settings"]
    assert saved["events"]["approval"] == "ping" and saved["digest"] == {"day": "fri", "time": "09:30"}
    assert test_client.get("/api/notifications").json()["settings"] == saved
    assert test_client.post("/api/notifications/test").status_code == 409
    test_client.put("/api/notifications/targets", json={"discord_webhook": HOOK})
    sent = []
    monkeypatch.setattr(notify_core, "send", lambda targets, title, message, link, mode="post", **kw: sent.append(mode) or 1)
    assert test_client.post("/api/notifications/test").json() == {"sent": 1} and sent == ["ping"]


def test_digest_preview(client):
    test_client, fake, tmp_path = client
    fake.hashes["sid:goals:g1"] = {"id": "g1", "project_id": "shop", "status": "completed", "updated_at": "9999999999"}
    preview = test_client.get("/api/notifications/digest-preview").json()
    assert preview["title"].startswith("SID weekly digest") and "**Shop**: 1 goal done" in preview["text"]
