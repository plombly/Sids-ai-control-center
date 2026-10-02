"""Sign-in: setup lock, administrator creation with the setup code,
sessions, devices, origin checks, rate limits and the audit log."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

import auth
import main
from test_device_routes import DeviceRedis


class AuthRedis(DeviceRedis):
    def incr(self, key):
        self.strings[key] = str(int(self.strings.get(key) or 0) + 1)
        return int(self.strings[key])

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start:end + 1]

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start:end + 1]

    def srem(self, key, *members):
        self.sets.get(key, set()).difference_update(members)


@pytest.fixture
def api(monkeypatch):
    fake = AuthRedis({"laika:projects:shop": {"id": "shop", "name": "Shop", "status": "active"}}, members=["shop"])
    fake.lists = {}
    fake.strings["laika:setup:code"] = hashlib.sha256(b"ABCD2345").hexdigest()
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(main, "OPERATOR_TOKEN", "")
    monkeypatch.setenv("LAIKA_SETUP_LOCK", "1")
    return TestClient(main.app, base_url="http://laika.lan:8080"), fake


def setup(client):
    return client.post("/api/setup/admin", json={"code": "abcd-2345", "username": "dylan", "password": "correct horse battery"})


def test_everything_is_locked_until_setup(api):
    client, _ = api
    assert client.get("/health").status_code == 200
    assert client.get("/api/auth/state").json()["setup_required"] is True
    locked = client.get("/api/projects")
    assert locked.status_code == 401 and locked.json()["setup_required"] is True
    assert client.post("/api/projects/shop/goals", json={"goal": "x"}).status_code == 401


def test_setup_needs_the_code_creates_the_admin_and_signs_in(api):
    client, fake = api
    bad = client.post("/api/setup/admin", json={"code": "WRONG-CODE", "username": "dylan", "password": "correct horse battery"})
    assert bad.status_code == 403
    short = client.post("/api/setup/admin", json={"code": "ABCD-2345", "username": "dylan", "password": "short"})
    assert short.status_code == 422
    assert setup(client).status_code == 200
    assert "laika:setup:code" not in fake.strings
    assert fake.hashes["laika:auth:admin"]["password"].startswith("scrypt$") and "correct horse" not in json.dumps(fake.hashes)
    assert client.get("/api/projects").status_code == 200  # the session cookie works
    assert client.get("/api/auth/state").json() == {"setup_required": False, "admin_exists": True, "signed_in": True, "user": "dylan"}
    assert setup(client).status_code == 409


def test_sign_in_out_wrong_passwords_and_rate_limit(api):
    client, fake = api
    setup(client)
    client.post("/api/auth/logout")
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/auth/login", json={"username": "dylan", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "dylan", "password": "correct horse battery"}).status_code == 200
    assert client.get("/api/projects").status_code == 200
    client.post("/api/auth/logout")
    for _ in range(auth.FAILS_ALLOWED):
        client.post("/api/auth/login", json={"username": "dylan", "password": "nope"})
    assert client.post("/api/auth/login", json={"username": "dylan", "password": "correct horse battery"}).status_code == 429


def test_writes_from_other_sites_are_refused_and_writes_are_audited(api):
    client, fake = api
    setup(client)
    evil = client.patch("/api/projects/shop", json={"importance": "low"}, headers={"Origin": "http://evil.example"})
    assert evil.status_code == 403
    # Through nginx, Host keeps the port; behind another proxy, X-Forwarded-Host counts.
    portless = client.patch("/api/projects/shop", json={"importance": "low"},
                            headers={"Origin": "http://laika.lan:8080", "Host": "laika.lan"})
    assert portless.status_code == 403
    proxied = client.patch("/api/projects/shop", json={"importance": "low"},
                           headers={"Origin": "https://laika.home", "Host": "10.0.0.5:8080", "X-Forwarded-Host": "laika.home"})
    assert proxied.status_code == 200
    ok = client.patch("/api/projects/shop", json={"importance": "low"}, headers={"Origin": "http://laika.lan:8080"})
    assert ok.status_code == 200
    entries = client.get("/api/audit").json()["entries"]
    assert entries[0]["path"] == "/api/projects/shop" and entries[0]["actor"] == "user dylan" and entries[0]["status"] == 200
    assert any("created" in e["actor"] for e in entries)


def test_devices_and_the_operator_token_still_work(api, monkeypatch):
    client, fake = api
    setup(client)
    created = client.post("/api/devices", json={"name": "Phone", "url": "http://10.0.0.5:8000"}).json()
    phone = TestClient(main.app)
    assert phone.get("/api/projects").status_code == 401
    assert phone.get("/api/projects", headers={"Authorization": f"Bearer {created['key']}"}).status_code == 200
    monkeypatch.setattr(main, "OPERATOR_TOKEN", "tok")
    assert phone.get("/api/projects", headers={"X-Laika-Token": "tok"}).status_code == 200


def test_password_change_and_sessions(api):
    client, fake = api
    setup(client)
    other = TestClient(main.app, base_url="http://laika.lan:8080")
    other.post("/api/auth/login", json={"username": "dylan", "password": "correct horse battery"})
    assert len(client.get("/api/auth/sessions").json()["sessions"]) == 2
    assert client.post("/api/auth/password", json={"current": "nope", "new": "another long password"}).status_code == 403
    assert client.post("/api/auth/password", json={"current": "correct horse battery", "new": "another long password"}).status_code == 200
    assert other.get("/api/projects").status_code == 401  # signed out everywhere else
    assert client.get("/api/projects").status_code == 200
    assert auth.verify_password("another long password", fake.hashes["laika:auth:admin"]["password"])


def test_setup_info_and_done(api):
    client, fake = api
    setup(client)
    fake.strings["laika:host-info"] = json.dumps({"cpus": 8, "memory_gb": 7.1, "public": ["93.184.216.34"]})
    info = client.get("/api/setup/info").json()
    assert info["suggested_workers"] == 7 and info["public_addresses"] == ["93.184.216.34"]
    assert client.get("/api/setup/state").json()["done"] is False
    assert client.post("/api/setup/done").status_code == 200
    assert client.get("/api/setup/state").json()["done"] is True
