"""Teams (LAIka 1.1): administrators and members with per-project access."""

import json

import pytest
from fastapi.testclient import TestClient

import access
import main
from test_auth import AuthRedis

PASSWORD = "correct horse battery"


@pytest.fixture
def team(monkeypatch):
    fake = AuthRedis({
        "laika:projects:shop": {"id": "shop", "name": "Shop", "status": "active"},
        "laika:projects:shop-api": {"id": "shop-api", "name": "Shop API", "status": "active", "parent": "shop"},
        "laika:projects:blog": {"id": "blog", "name": "Blog", "status": "active"},
        "laika:jobs:jshop1": {"id": "jshop1", "project_id": "shop", "role": "builder", "status": "awaiting_review",
                              "title": "Cart", "created_at": "1"},
        "laika:jobs:japi1": {"id": "japi1", "project_id": "shop-api", "role": "builder", "status": "running",
                             "title": "Orders", "created_at": "2"},
        "laika:jobs:jblog1": {"id": "jblog1", "project_id": "blog", "role": "builder", "status": "running",
                              "title": "Secret blog work", "created_at": "3"},
        "laika:workers:laika-worker-01": {"id": "laika-worker-01", "status": "working", "job_id": "jblog1"},
    }, members=["shop", "shop-api", "blog"])
    fake.lists = {}
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(main, "OPERATOR_TOKEN", "")
    monkeypatch.setenv("LAIKA_SETUP_LOCK", "1")
    # LAIka 1.0's single administrator: becomes the first account.
    import auth
    fake.hashes["laika:auth:admin"] = {"username": "Alex", "password": auth.hash_password(PASSWORD), "created_at": "1"}
    admin = TestClient(main.app, base_url="http://laika.lan:8080")
    assert admin.post("/api/auth/login", json={"username": "alex", "password": PASSWORD}).status_code == 200
    return admin, fake


def invite(admin, username, role="member", grants=None):
    made = admin.post("/api/users", json={"username": username, "role": role, "access": grants or {}})
    assert made.status_code == 201, made.text
    return made.json()["invite"]["token"]


def join(token, password=PASSWORD):
    client = TestClient(main.app, base_url="http://laika.lan:8080")
    assert client.get(f"/api/invites/{token}").json()["valid"] is True
    accepted = client.post(f"/api/invites/{token}", json={"password": password})
    assert accepted.status_code == 200, accepted.text
    return client


def test_the_10_administrator_becomes_the_first_account(team):
    admin, fake = team
    assert "laika:auth:admin" not in fake.hashes and fake.hashes["laika:users:alex"]["role"] == "admin"
    me = admin.get("/api/me").json()
    assert me["admin"] is True and me["projects"] is None and me["username"] == "Alex"


def test_an_invited_member_sees_only_their_projects_and_children(team):
    admin, fake = team
    sam = join(invite(admin, "Sam", grants={"shop": "view"}))
    listed = {p["id"]: p for p in sam.get("/api/projects").json()}
    assert set(listed) == {"shop", "shop-api"} and listed["shop-api"]["my_access"] == "view"
    assert sam.get("/api/projects/blog").status_code == 404           # no hint it exists
    assert sam.get("/api/jobs/jblog1").status_code == 404
    jobs = sam.get("/api/jobs?limit=100").json()
    assert {j["id"] for j in jobs} <= {"jshop1", "japi1"} and "jblog1" not in json.dumps(jobs)
    workers = sam.get("/api/workers").json()
    assert "jblog1" not in json.dumps(workers)                         # busy, but not on what
    assert sam.get("/api/me").json()["projects"] == {"shop": "view", "shop-api": "view"}


def test_levels_decide_what_a_member_may_do(team):
    admin, fake = team
    viewer = join(invite(admin, "vic", grants={"shop": "view"}))
    builder = join(invite(admin, "bea", grants={"shop": "build"}))
    for client, allowed in ((viewer, False), (builder, True)):
        refused = client.post("/api/projects/shop/builds").status_code == 403
        assert refused is not allowed
    assert builder.patch("/api/projects/shop", json={"importance": "low"}).status_code == 403   # settings: approve
    assert builder.get("/api/projects/shop/env").status_code == 403                              # secrets: approve
    approve = {"action": "approve", "request_id": "req-00000001", "expected_status": "awaiting_review",
               "expected_candidate": "c" * 40}
    assert builder.post("/api/jobs/jshop1/actions", json=approve).json()["detail"] == "You need approve access to this project"
    for path in ("/api/settings", "/api/users", "/api/audit", "/api/ai-providers", "/api/notifications"):
        assert builder.get(path).status_code == 403, path
    assert builder.post("/api/workers/scale", json={"delta": 1}).status_code == 403
    assert builder.post("/api/projects/shop/delete", json={"request_id": "del-00000001"}).status_code == 403


def test_unlisted_endpoints_are_administrator_only():
    assert access.rule("GET", "/api/something-new") == ("admin",)
    assert access.rule("POST", "/api/projects/shop/brand-new-action") == ("admin",)
    assert access.rule("GET", "/api/projects/shop/history")[3] == "view"


def test_people_management_keeps_an_administrator(team):
    admin, fake = team
    assert admin.delete("/api/users/alex").status_code == 409                          # not yourself
    assert admin.patch("/api/users/alex", json={"role": "member"}).status_code == 409  # the last administrator
    assert admin.post("/api/users", json={"username": "ALEX"}).status_code == 409      # names ignore case
    assert admin.post("/api/users", json={"username": "x", "access": {"nope": "view"}}).status_code == 422
    assert admin.post("/api/users", json={"username": "kim", "access": {"shop": "owner"}}).status_code == 422
    kim = join(invite(admin, "kim", grants={"shop": "build"}))
    assert kim.get("/api/projects").status_code == 200
    assert admin.patch("/api/users/kim", json={"disabled": True}).status_code == 200
    assert kim.get("/api/projects").status_code == 401                                 # signed out at once
    users = {u["name"]: u for u in admin.get("/api/users").json()["users"]}
    assert users["kim"]["disabled"] is True and "password" not in json.dumps(users)


def test_invites_are_single_use_and_a_new_one_resets_sign_in(team):
    admin, fake = team
    token = invite(admin, "lee", grants={"blog": "approve"})
    lee = join(token)
    assert TestClient(main.app).get(f"/api/invites/{token}").json() == {"valid": False}
    fresh = admin.post("/api/users/lee/invite").json()["invite"]["token"]
    assert lee.get("/api/projects").status_code == 401                   # old sign-in ended
    assert TestClient(main.app, base_url="http://laika.lan:8080").post(
        "/api/auth/login", json={"username": "lee", "password": PASSWORD}).status_code == 401  # old password gone
    join(fresh, "another long password")


def test_members_see_their_own_sessions_and_requests_only(team):
    admin, fake = team
    sam = join(invite(admin, "sam", grants={"shop": "build"}))
    assert {s["user"] for s in sam.get("/api/auth/sessions").json()["sessions"]} == {"sam"}
    assert len(admin.get("/api/auth/sessions").json()["sessions"]) == 2
    fake.hashes["laika:operator-results:req-of-alex"] = {"request_id": "req-of-alex", "status": "pending", "requested_by": "alex"}
    assert sam.get("/api/operator-requests/req-of-alex").status_code == 404
    assert admin.get("/api/operator-requests/req-of-alex").status_code == 200


def test_a_phone_acts_for_whoever_paired_it(team):
    admin, fake = team
    sam = join(invite(admin, "sam", grants={"shop": "view"}))
    paired = sam.post("/api/devices", json={"name": "Sam's phone", "url": "http://192.168.1.20:8080"})
    assert paired.status_code == 201
    phone = TestClient(main.app, headers={"Authorization": f"Bearer {paired.json()['key']}"})
    assert {p["id"] for p in phone.get("/api/projects").json()} == {"shop", "shop-api"}
    assert [d["owner"] for d in sam.get("/api/devices").json()["devices"]] == ["sam"]
    admin.delete("/api/users/sam")
    assert phone.get("/api/projects").status_code == 401                 # removed with its owner
