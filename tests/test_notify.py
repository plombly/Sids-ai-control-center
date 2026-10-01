"""scripts/sid-notify.py: one notification per new event, nothing old."""

import json

import pytest

from sid_testing import MemoryRedis, ROOT, load_module


class NotifyRedis(MemoryRedis):
    def type(self, key):
        if key in self.records:
            return "hash"
        value = self.values.get(key)
        return "list" if isinstance(value, list) else "string" if value is not None else "none"

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def zscore(self, key, member):
        return self.zsets.get(key, {}).get(member)

    def zremrangebyscore(self, key, low, high):
        zset = self.zsets.setdefault(key, {})
        for member in [m for m, score in zset.items() if score <= high]:
            del zset[member]


CONFIG = {"NTFY_URL": "https://ntfy.example/topic", "DASHBOARD_URL": "http://sid:8080"}


@pytest.fixture
def notify():
    module = load_module(ROOT / "scripts/sid-notify.py")
    r = NotifyRedis()
    outbox = []
    sender = lambda config, title, message, link, priority="default", major=False: outbox.append((title, message, link, priority, major)) or 1
    return module, r, outbox, sender


def ready_job(r, job_id, project="shop", commit="c1"):
    r.records[f"sid:jobs:{job_id}"] = {
        "id": job_id, "project_id": project, "title": "Add a pricing page", "status": "awaiting_review",
        "review_status": "complete", "review_verdict": "pass", "integration_status": "passed",
        "reviewed_commit": commit, "integrated_candidate_commit": commit}


def test_first_run_is_silent_then_only_new_events_are_sent_once(notify):
    module, r, outbox, sender = notify
    ready_job(r, "old")
    assert module.run(r, CONFIG, now=1000, sender=sender)["first_run"] and outbox == []
    ready_job(r, "new")
    r.records["sid:goals:g1"] = {"id": "g1", "status": "completed", "project_id": "shop", "prompt": "Build the shop"}
    module.run(r, CONFIG, now=1060, sender=sender)
    titles = sorted(title for title, *_ in outbox)
    assert not any(major for *_, major in outbox)  # approvals and finished goals do not ping
    assert titles == ["Goal finished · shop", "Ready for approval · shop"]
    module.run(r, CONFIG, now=1120, sender=sender)
    assert len(outbox) == 2  # not repeated


def test_queued_approvals_and_reviews_of_old_candidates_are_not_announced(notify):
    module, r, outbox, sender = notify
    module.run(r, CONFIG, now=1, sender=sender)
    ready_job(r, "q1")
    r.values["sid:merge-queue:shop"] = ["q1"]
    ready_job(r, "stale")
    r.records["sid:jobs:stale"]["reviewed_commit"] = "older"
    module.run(r, CONFIG, now=2, sender=sender)
    assert outbox == []


def test_needs_human_app_crash_backup_and_health(notify):
    module, r, outbox, sender = notify
    module.run(r, CONFIG, now=1, sender=sender)
    r.records["sid:jobs:b2"] = {"id": "b2", "status": "needs_human", "needs_human_kind": "build", "title": "Fix login"}
    r.records["sid:app-status:shop"] = {"state": "crashed", "commit": "abc", "error": "the app keeps exiting"}
    r.values["sid:backup:last"] = json.dumps({"at": "20261001T033000Z", "ok": False, "errors": {"postgres": "dump failed"}})
    r.values["sid:health"] = json.dumps({"status": "fail", "checks": [{"name": "disk", "level": "fail", "detail": "95% used"}]})
    module.run(r, CONFIG, now=2, sender=sender)
    by_title = {title: (message, priority) for title, message, link, priority, major in outbox}
    assert all(major for *_, major in outbox)  # every one of these is a major issue
    assert by_title["Needs you · sid"][0] == "Fix login (gave up after build attempts)"
    assert by_title["App crashed · shop"] == ("the app keeps exiting", "high")
    assert "postgres: dump failed" in by_title["Backup failed"][0]
    assert by_title["SID health is red"][0] == "disk: 95% used"


def test_failed_delivery_is_retried_and_no_target_means_silence(notify):
    module, r, outbox, sender = notify
    module.run(r, CONFIG, now=1, sender=sender)
    ready_job(r, "j1")
    assert module.run(r, CONFIG, now=2, sender=lambda *a, **k: 0)["sent"] == []
    assert module.run(r, CONFIG, now=3, sender=sender)["sent"] == ["approval:j1:c1"]
    ready_job(r, "j2")
    unconfigured = {"DASHBOARD_URL": "http://sid:8080"}
    assert module.run(r, unconfigured, now=4, sender=sender)["sent"] == []
    module.run(r, CONFIG, now=5, sender=sender)
    assert len(outbox) == 1  # j2 happened before a target was configured: not sent later


def test_send_formats_ntfy_and_discord():
    module = load_module(ROOT / "scripts/sid-notify.py")
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(request, timeout):
        requests.append(request)
        return Response()
    config = {"NTFY_URL": "https://ntfy.example/t", "DISCORD_WEBHOOK": "https://discord.example/hook"}
    assert module.send(config, "Ready · shop", "Add page", "http://sid:8080/#/", "high", opener=opener) == 2
    ntfy, discord = requests
    assert ntfy.full_url == "https://ntfy.example/"
    assert json.loads(ntfy.data) == {"topic": "t", "title": "Ready · shop", "message": "Add page",
                                     "click": "http://sid:8080/#/", "priority": 4, "tags": ["robot"]}
    assert json.loads(discord.data) == {"content": "**Ready · shop**\nAdd page\nhttp://sid:8080/#/",
                                        "allowed_mentions": {"parse": [], "users": []}}
    requests.clear()
    config = {"DISCORD_WEBHOOK": "https://discord.example/hook", "DISCORD_MENTION": "269981261508902923"}
    module.send(config, "App crashed · shop", "@everyone look", "http://sid", "high", major=True, opener=opener)
    body = json.loads(requests[0].data)
    assert body["content"].startswith("<@269981261508902923> **App crashed")
    assert body["allowed_mentions"] == {"parse": [], "users": ["269981261508902923"]}
