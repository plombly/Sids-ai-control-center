"""AI providers: status, sign-in requests, Claude code hand-off, write-only keys."""

import json

import pytest
from fastapi.testclient import TestClient

import main
import provider_routes
from test_device_routes import DeviceRedis


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = DeviceRedis({"laika:operator-service:op": {"id": "op",
                       "allowed_actions": "provider_login,provider_status,provider_apply_keys"}}, members=[])
    fake.strings["laika:providers:status"] = json.dumps({"claude": {"signed_in": True, "plan": "pro"}, "codex": {"signed_in": False}})
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(main, "_operator_service", lambda: fake.hashes["laika:operator-service:op"])
    monkeypatch.setattr(provider_routes, "PROVIDERS_DIR", tmp_path)
    return TestClient(main.app), fake, tmp_path


def test_status_login_and_claude_code(api):
    client, fake, _ = api
    body = client.get("/api/ai-providers").json()
    assert body["stale"] is True  # never checked
    assert body["status"]["claude"]["signed_in"] is True and body["keys"] == {"anthropic_api_key": False, "openai_api_key": False}
    started = client.post("/api/ai-providers/codex/login", json={"request_id": "login-0001"})
    assert started.status_code == 202 and fake.stream[-1][1]["action"] == "provider_login" and fake.stream[-1][1]["what"] == "codex"
    assert client.post("/api/ai-providers/gemini/login", json={"request_id": "login-0002"}).status_code == 404
    assert client.post("/api/ai-providers/claude/code", json={"code": "abc123#xyz"}).status_code == 409
    fake.strings["laika:provider-login:claude"] = json.dumps({"state": "waiting", "url": "https://claude.ai/x"})
    assert client.post("/api/ai-providers/claude/code", json={"code": "abc123#xyz"}).status_code == 200
    assert fake.strings["laika:provider-login:claude:code"] == "abc123#xyz"
    assert client.get("/api/ai-providers").json()["login"]["claude"]["url"] == "https://claude.ai/x"
    import time
    fake.strings["laika:providers:status"] = json.dumps({"claude": {"signed_in": False}, "checked_at": time.time()})
    assert client.get("/api/ai-providers").json()["stale"] is False


def test_keys_are_write_only_and_validated(api):
    client, fake, folder = api
    assert client.put("/api/ai-providers/keys", json={"openai_api_key": "not-a-key"}).status_code == 422
    saved = client.put("/api/ai-providers/keys", json={"anthropic_api_key": "sk-ant-" + "a" * 30})
    assert saved.json()["keys"] == {"anthropic_api_key": True, "openai_api_key": False}
    text = (folder / "providers.env").read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-" in text and oct((folder / "providers.env").stat().st_mode)[-3:] == "600"
    assert "sk-ant" not in json.dumps(client.get("/api/ai-providers").json())
    client.put("/api/ai-providers/keys", json={"anthropic_api_key": ""})
    assert "ANTHROPIC_API_KEY" not in (folder / "providers.env").read_text()
