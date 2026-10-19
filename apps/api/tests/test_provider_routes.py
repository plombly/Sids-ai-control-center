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


class AccountsRedis(DeviceRedis):
    def delete(self, *keys):
        for key in keys:
            super().delete(key)

    def srem(self, key, *members):
        self.sets.setdefault(key, set()).difference_update(members)

    def zcount(self, key, low, high):
        return 0


@pytest.fixture
def accounts_api(monkeypatch, tmp_path):
    fake = AccountsRedis({"laika:operator-service:op": {"id": "op",
                         "allowed_actions": "provider_login,provider_status,provider_apply_keys"}}, members=[])
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(main, "_operator_service", lambda: fake.hashes["laika:operator-service:op"])
    monkeypatch.setattr(provider_routes, "PROVIDERS_DIR", tmp_path)
    return TestClient(main.app), fake, tmp_path


def test_accounts_are_added_changed_signed_in_and_removed(accounts_api):
    client, fake, folder = accounts_api
    listed = client.get("/api/ai-providers/accounts").json()
    assert [a["id"] for a in listed["claude"]] == ["default"] and listed["claude"][0]["max_concurrent"] == 2
    fake.strings["laika:provider-cooldown:claude"] = "claude quota: every account"
    made = client.post("/api/ai-providers/claude/accounts",
                       json={"id": "work", "label": "Work plan", "kind": "login", "max_concurrent": 3})
    assert made.status_code == 201 and made.json()["label"] == "Work plan" and made.json()["max_concurrent"] == 3
    assert "laika:provider-cooldown:claude" not in fake.strings         # a new account can serve
    assert client.post("/api/ai-providers/claude/accounts",
                       json={"id": "work", "label": "x", "kind": "login"}).status_code == 409
    assert client.post("/api/ai-providers/claude/accounts",
                       json={"id": "default", "label": "x", "kind": "login"}).status_code == 409
    assert client.post("/api/ai-providers/claude/accounts",
                       json={"id": "Bad Name", "label": "x", "kind": "login"}).status_code == 422
    assert client.patch("/api/ai-providers/claude/accounts/work", json={"enabled": False}).json()["enabled"] is False
    assert client.patch("/api/ai-providers/claude/accounts/default", json={"max_concurrent": 1}).json()["max_concurrent"] == 1
    login = client.post("/api/ai-providers/claude/accounts/work/login", json={"request_id": "login-acct-1"})
    assert login.status_code == 202 and fake.stream[-1][1]["what"] == "claude:work"
    assert client.post("/api/ai-providers/claude/accounts/nope/login", json={"request_id": "login-acct-2"}).status_code == 404
    assert client.delete("/api/ai-providers/claude/accounts/default").status_code == 409
    removed = client.delete("/api/ai-providers/claude/accounts/work").json()
    assert removed["deleted"] == "work" and fake.stream[-1][1]["action"] == "provider_apply_keys"
    assert [a["id"] for a in client.get("/api/ai-providers/accounts").json()["claude"]] == ["default"]


def test_an_extra_accounts_key_is_write_only_and_kept_apart(accounts_api):
    client, fake, folder = accounts_api
    client.post("/api/ai-providers/codex/accounts", json={"id": "second", "label": "Second", "kind": "key",
                                                          "max_concurrent": 0})
    assert client.put("/api/ai-providers/codex/accounts/second/key", json={"key": "sk-ant-" + "a" * 30}).status_code == 422
    saved = client.put("/api/ai-providers/codex/accounts/second/key", json={"key": "sk-" + "b" * 30})  # release-scan: allow (fake)
    assert saved.json() == {"key_saved": True, "applied": True}
    text = (folder / "accounts.env").read_text()
    assert "OPENAI_API_KEY__SECOND=sk-" in text and not (folder / "providers.env").exists()
    assert oct((folder / "accounts.env").stat().st_mode)[-3:] == "600"
    body = client.get("/api/ai-providers").json()
    assert body["accounts"]["codex"][1]["key_saved"] is True and "sk-bbb" not in json.dumps(body)
    client.delete("/api/ai-providers/codex/accounts/second")
    assert "OPENAI_API_KEY__SECOND" not in (folder / "accounts.env").read_text()
