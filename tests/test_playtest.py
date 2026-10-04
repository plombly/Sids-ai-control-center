"""scripts/laika-playtest.py and the apps service's play-test trigger."""

import json
import time

from laika_testing import ROOT, load_module


def test_events_become_errors_failed_requests_and_status():
    module = load_module(ROOT / "scripts/laika-playtest.py", "laika_playtest_test")
    base = "http://host.docker.internal:8100"
    events = [
        {"method": "Network.requestWillBeSent", "params": {"requestId": "1", "request": {"url": f"{base}/app.js"}}},
        {"method": "Network.loadingFailed", "params": {"requestId": "1", "errorText": "net::ERR_CONNECTION_REFUSED"}},
        {"method": "Network.responseReceived", "params": {"type": "Image", "response": {"status": 404, "url": f"{base}/a.png"}}},
        {"method": "Runtime.consoleAPICalled", "params": {"type": "error", "args": [{"value": "boom"}]}},
        {"method": "Runtime.consoleAPICalled", "params": {"type": "log", "args": [{"value": "fine"}]}},
        {"method": "Runtime.exceptionThrown", "params": {"exceptionDetails": {"exception": {"description": "TypeError: x\n at y"}}}},
    ]
    result = module.summarize(events, 200, base)
    assert result == {"console_errors": ["boom"], "page_errors": ["TypeError: x"], "status": 200,
                      "failed_requests": ["net::ERR_CONNECTION_REFUSED /app.js", "404 /a.png"]}


def test_a_browser_that_cannot_start_is_reported_not_raised():
    module = load_module(ROOT / "scripts/laika-playtest.py", "laika_playtest_test2")
    import subprocess
    calls = []
    def docker(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "no such image")
    result = module.run_test("http://x/", "/nonexistent/shot.png", docker=docker)
    assert result == {"ok": False, "error": "could not start the browser: no such image"}
    assert calls[0][:4] == ["docker", "run", "-d", "--rm"] and "--memory" in calls[0]


def test_the_apps_service_tests_each_new_deploy_of_a_web_page_once(tmp_path, monkeypatch):
    import subprocess
    from laika_testing import MemoryRedis
    apps = load_module(ROOT / "services/apps/laika_apps.py", "laika_apps_playtest_test")
    apps.redis = MemoryRedis()
    started = []
    monkeypatch.setattr(apps, "run", lambda argv, **k: started.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(apps, "unit_is_running", lambda unit: False)
    project = type("P", (), {"id": "game"})()

    class Page:
        def __init__(self, kind):
            self.headers = {"content-type": kind}
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    status = {"commit": "c1", "port": "8100", "deployed_at": str(time.time() - 60)}
    apps.maybe_playtest(project, status, opener=lambda url, timeout: Page("text/html; charset=utf-8"))
    assert started and started[0][1] == "--unit=laika-playtest-game" and started[0][-4:] == ["--port", "8100", "--commit", "c1"]
    apps.redis.values["laika:playtest:game"] = json.dumps({"commit": "c1", "ok": True})
    apps.maybe_playtest(project, status, opener=lambda url, timeout: Page("text/html"))
    assert len(started) == 1                                    # once per commit
    apps.maybe_playtest(project, {**status, "commit": "c2"}, opener=lambda url, timeout: Page("application/json"))
    assert len(started) == 1 and json.loads(apps.redis.values["laika:playtest:game"])["skipped"]   # an API: skipped
    apps.maybe_playtest(project, {**status, "commit": "c3", "deployed_at": str(time.time())}, opener=lambda u, timeout: Page("text/html"))
    assert len(started) == 1                                    # just deployed: wait a moment
