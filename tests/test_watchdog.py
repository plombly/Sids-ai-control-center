"""Watchdog: detects what silently stops the pipeline; publishes, never acts."""

import calendar
import json
import time
from types import SimpleNamespace

import pytest

from laika_testing import MemoryRedis, ROOT, load_module

NOW = calendar.timegm((2026, 9, 30, 12, 0, 0, 0, 0, 0))


class HealthRedis(MemoryRedis):
    def ping(self):
        return True


@pytest.fixture
def wd():
    return load_module(ROOT / "scripts/laika-watchdog.py")


def healthy_redis(wd):
    r = HealthRedis()
    r.records["laika:orchestrators:o1"] = {"id": "o1"}
    r.records["laika:operator-service:op"] = {"id": "op"}
    for n in range(1, wd.EXPECTED_WORKERS + 1):
        r.records[f"laika:workers:w{n}"] = {"status": "idle"}
    r.values["laika:backup:last"] = json.dumps({"at": "20260930T100000Z", "ok": True})
    r.values["laika:backup:restore-check"] = json.dumps({"at": 1790769600 - 86400, "ok": True, "snapshot": "s1", "checks": []})
    return r


def runner_for(units_active=True, dirty=""):
    def run(cmd, **kwargs):
        if cmd[0] == "systemctl":
            return SimpleNamespace(stdout="\n".join("active" if units_active else "failed" for _ in cmd[2:]),
                                   returncode=0)
        return SimpleNamespace(stdout=dirty, stderr="", returncode=0)
    return run


def run(wd, r, **kw):
    kw.setdefault("runner", runner_for())
    kw.setdefault("getter", lambda url: {"status": "healthy"})
    kw.setdefault("usage", lambda path: (100, 50, 50))
    kw.setdefault("meminfo_reader", lambda: "MemTotal: 8388608 kB\nMemAvailable: 6291456 kB\n")
    kw.setdefault("loadavg", lambda: (1.0, 1.2, 1.4))
    kw.setdefault("cpu_count", lambda: 4)
    return wd.run_checks(r, now=NOW, **kw)


def levels(report):
    return {c["name"]: c["level"] for c in report["checks"]}


def test_all_healthy(wd):
    report = run(wd, healthy_redis(wd))
    assert report["status"] == "ok", report
    assert set(levels(report)) == {"units", "redis", "heartbeats", "backup", "pipeline",
                                   "api", "web", "live_tree", "disk", "memory", "load", "restore_check", "exposure"}


def test_backup_age_is_computed_in_utc(wd):
    report = run(wd, healthy_redis(wd))
    backup = next(c for c in report["checks"] if c["name"] == "backup")
    assert backup["detail"] == "last backup 2.0h ago"


@pytest.mark.parametrize("mutate,name,level", [
    (lambda r: r.records.pop("laika:orchestrators:o1"), "heartbeats", "fail"),
    (lambda r: r.records.pop("laika:operator-service:op"), "heartbeats", "fail"),
    (lambda r: r.records.pop("laika:workers:w1"), "heartbeats", "fail"),
    (lambda r: r.values.pop("laika:backup:last"), "backup", "warn"),
    (lambda r: r.values.pop("laika:backup:restore-check"), "restore_check", "warn"),
    (lambda r: r.values.update({"laika:backup:restore-check": json.dumps({"at": 1790769600 - 50 * 86400, "ok": True})}), "restore_check", "warn"),
    (lambda r: r.values.update({"laika:backup:restore-check": json.dumps({"at": 1790769600, "ok": False, "snapshot": "s2",
                                                                        "checks": [{"name": "redis", "ok": False, "detail": "empty"}]})}), "restore_check", "fail"),
    (lambda r: r.values.update({"laika:backup:last": json.dumps({"at": "20260928T000000Z", "ok": True})}), "backup", "warn"),
    (lambda r: r.values.update({"laika:backup:last": json.dumps({"at": "20260930T100000Z", "ok": False, "errors": {"redis": "x"}})}), "backup", "fail"),
    (lambda r: r.values.update({"laika:provider-cooldown:claude": "limit"}), "pipeline", "warn"),
    (lambda r: r.records.update({"laika:jobs:j1": {"status": "needs_human"}}), "pipeline", "warn"),
    (lambda r: r.records.update({"laika:jobs:j2": {"status": "awaiting_review", "merge_queue_state": "invalid"}}), "pipeline", "warn"),
])
def test_problems_are_detected(wd, mutate, name, level):
    r = healthy_redis(wd)
    mutate(r)
    report = run(wd, r)
    assert levels(report)[name] == level
    assert report["status"] in ("warn", "fail")


def test_down_unit_dirty_tree_bad_api_and_full_disk(wd):
    report = run(wd, healthy_redis(wd), runner=runner_for(units_active=False, dirty="?? stray.txt\n"),
                 getter=lambda url: {"status": "degraded"}, usage=lambda path: (100, 97, 3))
    lv = levels(report)
    assert (lv["units"], lv["live_tree"], lv["api"], lv["disk"]) == ("fail", "fail", "fail", "fail")
    tree = next(c for c in report["checks"] if c["name"] == "live_tree")
    assert "stray.txt" in tree["detail"]
    assert report["status"] == "fail"


def test_unreachable_endpoint(wd):
    def boom(url):
        raise OSError("connection refused")
    assert levels(run(wd, healthy_redis(wd), getter=boom))["web"] == "fail"


def test_publish_logs_only_changes(wd):
    r = healthy_redis(wd)
    first = wd.publish(r, run(wd, r))
    assert len(first) == 13, "every check is new the first time"
    assert wd.publish(r, run(wd, r)) == [], "no change, no log lines"
    r.values["laika:provider-cooldown:claude"] = "limit"
    changed = wd.publish(r, run(wd, r))
    assert changed == ["pipeline: ok -> warn (claude cooling down (limit))"]
    assert json.loads(r.values[wd.HEALTH_KEY])["status"] == "warn"


def test_watchdog_never_writes_job_state():
    source = (ROOT / "scripts/laika-watchdog.py").read_text()
    for forbidden in ("hset(", "hdel(", "rpush(", "delete(", "lrem("):
        assert forbidden not in source


@pytest.mark.parametrize("available,expected", [(6291456, "ok"), (1048576, "warn"), (524288, "fail")])
def test_memory_levels(wd, available, expected):
    result = wd.check_memory(lambda: f"MemTotal: 8388608 kB\nMemAvailable: {available} kB\n")
    assert result["level"] == expected
    if expected == "ok":
        assert result["detail"] == "6.0 GiB available of 8.0 GiB (75%)"


def test_memory_unreadable(wd):
    def unreadable():
        raise OSError("permission denied")
    result = wd.check_memory(unreadable)
    assert result["level"] == "warn"
    assert "cannot read /proc/meminfo: permission denied" in result["detail"]


@pytest.mark.parametrize("load5,expected", [(1.2, "ok"), (7.0, "warn"), (13.0, "fail")])
def test_load_levels(wd, load5, expected):
    result = wd.check_load(lambda: (1.0, load5, 1.4), lambda: 4)
    assert result["level"] == expected
    assert result["detail"] == f"load 1.00/{load5:.2f}/1.40 on 4 CPUs"


def test_loadavg_unreadable(wd):
    def unreadable():
        raise OSError("not available")
    result = wd.check_load(unreadable, lambda: 4)
    assert result["level"] == "warn"
    assert "cannot read load average: not available" in result["detail"]


def test_threshold_environment_override(wd, monkeypatch):
    monkeypatch.setenv("WATCHDOG_MEM_WARN_PERCENT", "80")
    assert wd.check_memory(lambda: "MemTotal: 100 kB\nMemAvailable: 75 kB\n")["level"] == "warn"


def test_system_info_hides_credentials_in_https_remotes():
    import subprocess as sp
    module = load_module(ROOT / "scripts/laika-watchdog.py")
    answers = {"get-url": "https://user:tok@github.com/me/laika.git", "symbolic-ref": "main", "rev-parse": "abc1234",
               "log": "Last commit"}
    def runner(args, **kwargs):
        key = next(k for k in answers if k in args)
        return sp.CompletedProcess(args, 0, answers[key] + "\n", "")
    info = module.system_info(runner)
    assert info["remote"] == "https://github.com/me/laika.git" and info["branch"] == "main" and info["head"] == "abc1234"


def test_a_public_address_is_a_warning(wd):
    import json as _json
    import subprocess as _sp
    out = _json.dumps([{"ifname": "eth0", "addr_info": [{"local": "93.184.216.34"}]},
                       {"ifname": "eth1", "addr_info": [{"local": "10.0.0.5"}]},
                       {"ifname": "docker0", "addr_info": [{"local": "172.17.0.1"}]}])
    info = wd.host_info(lambda argv, **k: _sp.CompletedProcess(argv, 0, out, ""), lambda: "MemTotal: 8388608 kB\n", lambda: 8)
    assert info["public"] == ["93.184.216.34"] and info["cpus"] == 8 and info["memory_gb"] == 8.0
    assert [a["interface"] for a in info["addresses"]] == ["eth0", "eth1"]
    assert wd.check_exposure(info)["level"] == "warn"
    assert wd.check_exposure({"public": []})["level"] == "ok"


def test_expected_workers_follow_the_scaler(wd):
    r = healthy_redis(wd)
    r.values["laika:scaler:target"] = "3"
    report = run(wd, r)
    units = next(c for c in report["checks"] if c["name"] == "units")
    assert units["level"] == "ok" and units["detail"] == "6 units active"  # 3 services + 3 workers
    assert next(c for c in report["checks"] if c["name"] == "heartbeats")["level"] == "ok"
    assert wd.expected_workers(type("R", (), {"get": lambda self, k: None})()) == wd.EXPECTED_WORKERS
