"""scripts/laika-remote.py (Tailscale) and doctor's remote check."""

import json
import subprocess

from laika_testing import ROOT, MemoryRedis, load_module


def fake_tailscale(answers):
    def runner(argv, **kwargs):
        key = " ".join(argv[1:3])
        return subprocess.CompletedProcess(argv, 0, answers.get(key, "{}"), "")
    return runner


def test_status_reads_tailscale_and_flags_anything_public():
    remote = load_module(ROOT / "scripts/laika-remote.py", "laika_remote_test")
    assert remote.status(which=lambda name: None) ["state"] == "not_installed"
    answers = {"version": "1.90.0\n", "status --json": json.dumps({
        "BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.5", "fd7a::5"], "DNSName": "laika.tail1.ts.net.", "HostName": "laika"},
        "CurrentTailnet": {"Name": "me@example.com", "MagicDNSEnabled": True}}),
        "funnel status": json.dumps({"AllowFunnel": {"laika:443": True}}), "serve status": "{}",
        "debug prefs": json.dumps({"RunSSH": False})}
    info = remote.status(runner=fake_tailscale(answers), which=lambda name: "/usr/bin/tailscale")
    assert (info["state"], info["dns_name"], info["ips"][0], info["magic_dns"]) == ("connected", "laika.tail1.ts.net", "100.64.0.5", True)
    assert info["funnel"] is True and info["serve"] is False and info["ssh"] is False


def test_login_publishes_the_sign_in_link_and_never_enables_ssh_or_dns(monkeypatch):
    remote = load_module(ROOT / "scripts/laika-remote.py", "laika_remote_test2")
    r = MemoryRedis()
    monkeypatch.setattr(remote.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(remote, "publish_status", lambda r: {"state": "connected", "dns_name": "laika.ts.net"})
    seen = {}

    class Process:
        def __init__(self, argv, **kwargs):
            seen["argv"] = argv
            self.stdout = iter(["\nTo authenticate, visit:\n", "\n\thttps://login.tailscale.com/a/1234abcd\n", "Success.\n"])

        def wait(self):
            return 0

        def kill(self):
            pass
    states = []
    real_put = remote.put
    monkeypatch.setattr(remote, "put", lambda r, key, **fields: states.append((key, fields)) or real_put(r, key, **fields))
    assert remote.login(r, popen=Process) == 0
    assert ("laika:remote:login", {"state": "waiting", "url": "https://login.tailscale.com/a/1234abcd",
                                   "message": "Open the link, sign in to Tailscale and approve this server"}) in states
    assert json.loads(r.values["laika:remote:login"])["state"] == "done"
    assert "--ssh=false" in seen["argv"] and "--accept-dns=false" in seen["argv"] and "--reset" in seen["argv"]
    assert seen["argv"][:2] == ["tailscale", "up"]  # never funnel or serve


def test_doctor_fails_when_funnel_publishes_the_server(monkeypatch):
    doc = load_module(ROOT / "scripts/laika-doctor.py", "laika_doctor_remote_test")
    monkeypatch.setattr(doc.shutil, "which", lambda name: "/usr/bin/tailscale")
    public = fake_tailscale({"funnel status": json.dumps({"AllowFunnel": {"x:443": True}})})
    assert doc.check_remote(public)["level"] == "fail"
    fine = fake_tailscale({"status --json": json.dumps({"BackendState": "Running"})})
    assert doc.check_remote(fine)["level"] == "ok"
    monkeypatch.setattr(doc.shutil, "which", lambda name: None)
    assert doc.check_remote(fine)["level"] == "ok"
