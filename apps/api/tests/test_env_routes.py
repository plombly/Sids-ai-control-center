"""Project secrets: write-only values, systemd-compatible file, root-only."""

import os
import stat

import pytest
from fastapi.testclient import TestClient

import env_routes
import main
from test_project_routes import FakeRedis


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(env_routes, "ENV_DIR", tmp_path / "project-env")
    fake = FakeRedis({"sid:projects:shop": {"id": "shop", "status": "active"}}, members={"shop"})
    monkeypatch.setattr(main, "redis", fake)
    with TestClient(main.app) as client:
        yield client, fake, tmp_path / "project-env"


def test_values_are_stored_but_never_returned(env):
    client, fake, folder = env
    secret = 'p@ss "w" \\x $HOME'
    assert client.put("/api/projects/shop/env", json={"name": "API_KEY", "value": secret}).json() == {
        "name": "API_KEY", "length": len(secret), "saved": True}
    listing = client.get("/api/projects/shop/env")
    assert listing.json()["variables"] == [{"name": "API_KEY", "length": len(secret)}]
    assert secret not in listing.text and "p@ss" not in listing.text
    assert env_routes.read_env("shop") == {"API_KEY": secret}
    path = folder / "shop.env"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert "restart_at" in fake.hashes["sid:projects:shop"]  # the app restarts with it
    assert client.delete("/api/projects/shop/env/API_KEY").json()["deleted"]
    assert env_routes.read_env("shop") == {}


@pytest.mark.parametrize("body,code", [
    ({"name": "PORT", "value": "1"}, 422), ({"name": "1BAD", "value": "x"}, 422), ({"name": "A-B", "value": "x"}, 422),
    ({"name": "OK", "value": "two\nlines"}, 422), ({"name": "OK", "value": "x" * 9000}, 422),
])
def test_bad_names_and_values_are_refused(env, body, code):
    client, *_ = env
    assert client.put("/api/projects/shop/env", json=body).status_code == code


def test_sid_and_unknown_projects_have_no_env(env):
    client, *_ = env
    assert client.get("/api/projects/sid/env").status_code == 404
    assert client.put("/api/projects/ghost/env", json={"name": "A", "value": "b"}).status_code == 404
    assert client.delete("/api/projects/shop/env/NOPE").status_code == 404
