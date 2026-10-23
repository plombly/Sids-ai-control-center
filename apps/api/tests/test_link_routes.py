"""LAIka and LNMT working together (link_routes.py): pairing, signing in to
LNMT with a LAIka account, LNMT starting a project for a person, and a
project deploying to an LNMT app."""

import base64
import json

import pytest
from fastapi.testclient import TestClient

import access
import link_routes
import main
import project_routes
from test_teams import PASSWORD, invite, join, team  # noqa: F401  (fixture)

LNMT_TOKEN = "lnmtlink_" + "a" * 40


def code(url="https://10.0.0.232:8443"):
    raw = json.dumps({"v": 1, "name": "LNMT", "url": url, "token": LNMT_TOKEN, "cert_sha256": "b" * 64})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@pytest.fixture
def linked(team, monkeypatch, tmp_path):
    admin, fake = team
    fake.set = lambda key, value, ex=None, nx=False: fake.strings.__setitem__(key, value) or True
    fake.sismember = lambda key, member: member in fake.smembers(key)
    monkeypatch.setattr(link_routes, "LINK_DIR", tmp_path / "link")
    calls = []

    def fake_lnmt(method, path, body=None, conn=None, timeout=60):
        calls.append((method, path, body, conn["token_out"] if conn else None))
        if path == "/api/link/hello":
            return {"ok": True}
        if path.startswith("/api/link/apps"):
            return {"apps": [{"customer": "sam", "name": "site", "git_url": "ssh://c-sam@10.0.0.121/site.git", "sites": []}]}
        return {"ok": True}
    monkeypatch.setattr(link_routes, "call_lnmt", fake_lnmt)
    connected = admin.post("/api/link/lnmt", json={"code": code()})
    assert connected.status_code == 200, connected.text
    hello = calls[-1]
    return admin, fake, calls, hello[2]["token"]


def test_pairing_stores_tokens_root_only_and_never_shows_them(linked, tmp_path):
    admin, fake, calls, laika_token = linked
    method, path, body, sent_with = calls[0]
    assert (method, path, sent_with) == ("POST", "/api/link/hello", LNMT_TOKEN)
    assert body["laika_url"] == "http://laika.lan:8080" and body["token"].startswith("lnmtlink_")
    stored = tmp_path / "link" / "lnmt.json"
    assert oct(stored.stat().st_mode)[-3:] == "600"
    assert laika_token not in stored.read_text()                    # only its hash is kept
    status = admin.get("/api/link/lnmt").json()
    assert status["connected"] and LNMT_TOKEN not in json.dumps(status)
    assert admin.post("/api/link/lnmt", json={"code": "not a code at all, really"}).status_code == 422


def test_sign_in_to_lnmt_with_a_laika_account(linked):
    admin, fake, calls, laika_token = linked
    sam = join(invite(admin, "Sam"))
    answer = sam.post("/api/connect/authorize", json={"state": "s" * 20}, headers={"Origin": "http://laika.lan:8080"})
    assert answer.status_code == 200, answer.text
    redirect = answer.json()["redirect"]
    assert redirect.startswith("https://10.0.0.232:8443/api/laika/return?code=") and redirect.endswith("&state=" + "s" * 20)
    one_time = redirect.split("code=")[1].split("&")[0]
    service = TestClient(main.app, base_url="http://laika.lan:8080")
    assert service.post("/api/link/service/token", json={"code": one_time}).status_code == 401          # no token
    assert service.post("/api/link/service/token", json={"code": one_time},
                        headers={"Authorization": "Bearer lnmtlink_wrong"}).status_code == 401
    who = service.post("/api/link/service/token", json={"code": one_time}, headers={"Authorization": f"Bearer {laika_token}"})
    assert who.json() == {"username": "Sam", "name": "sam", "role": "member"}
    again = service.post("/api/link/service/token", json={"code": one_time}, headers={"Authorization": f"Bearer {laika_token}"})
    assert again.status_code == 400                                                                    # one use
    users = service.get("/api/link/service/users", headers={"Authorization": f"Bearer {laika_token}"}).json()["users"]
    assert {u["name"] for u in users} == {"alex", "sam"}


def test_lnmt_starts_a_project_for_a_person(linked, monkeypatch):
    admin, fake, calls, laika_token = linked
    join(invite(admin, "Sam"))
    queued = []
    monkeypatch.setattr(project_routes, "_operator_request", lambda action, rid, fields: queued.append((action, rid, fields, access.current_name())) or {})
    monkeypatch.setattr(project_routes, "_known", lambda pid: False)
    monkeypatch.setattr(link_routes, "_wait", lambda rid, seconds=90: {"status": "succeeded",
                        "output": json.dumps({"public_key": "ssh-ed25519 AAAAkey laika"})})
    service = TestClient(main.app, base_url="http://laika.lan:8080")
    made = service.post("/api/link/service/projects", headers={"Authorization": f"Bearer {laika_token}"}, json={
        "id": "devyn-site", "name": "Devyn's site", "template": "wordpress-theme", "for_user": "sam",
        "git_url": "ssh://c-sam@10.0.0.121/site.git", "request_id": "req-12345678"})
    assert made.status_code == 200, made.text
    assert made.json()["public_key"] == "ssh-ed25519 AAAAkey laika"
    assert [q[0] for q in queued] == ["create_project", "project_push_setup"]
    assert queued[0][2]["template"] == "wordpress-theme" and queued[0][3] == "sam"           # requested by the person
    assert queued[1][2]["url"] == "ssh://c-sam@10.0.0.121/site.git"
    assert access.get_user(fake, "sam")["access"] == {"devyn-site": "approve"}
    refused = service.post("/api/link/service/projects", headers={"Authorization": f"Bearer {laika_token}"}, json={
        "id": "other", "name": "x", "for_user": "nobody", "git_url": "ssh://c-x@h/x.git", "request_id": "req-87654321"})
    assert refused.status_code == 404


def test_a_project_deploys_to_the_persons_lnmt_app(linked, monkeypatch):
    admin, fake, calls, laika_token = linked
    sam = join(invite(admin, "Sam", grants={"shop": "approve"}))
    monkeypatch.setattr(link_routes, "_push_to_lnmt", lambda pid, url, rid: "ssh-ed25519 AAAAkey laika")
    origin = {"Origin": "http://laika.lan:8080"}
    missing = sam.post("/api/projects/shop/deploy-lnmt", json={"customer": "sam", "app": "nope", "request_id": "req-11111111"}, headers=origin)
    assert missing.status_code == 404
    done = sam.post("/api/projects/shop/deploy-lnmt", json={"customer": "sam", "app": "site", "request_id": "req-22222222"}, headers=origin)
    assert done.status_code == 200, done.text
    assert calls[-1][1] == "/api/link/deploy-key" and calls[-1][2]["user"] == "sam" and calls[-1][2]["label"] == "laika-shop"
    viewer = join(invite(admin, "Vic", grants={"shop": "view"}))
    assert viewer.post("/api/projects/shop/deploy-lnmt", json={"customer": "sam", "app": "site", "request_id": "req-33333333"},
                       headers=origin).status_code == 403
