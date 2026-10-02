"""Updates: signed manifests only, versions compared properly, safe unpacking."""

import hashlib
import io
import json
import subprocess
import tarfile

import pytest

from laika_testing import ROOT, MemoryRedis, load_module


@pytest.fixture
def up():
    return load_module(ROOT / "scripts/laika-update.py", "laika_update_test")


def test_versions(up):
    order = ["garbage", "0.9.0", "1.0.0-dev", "1.0.0-rc1", "1.0.0", "1.0.1", "1.10.0", "2.0.0"]
    assert sorted(order, key=up.version_key) == order


def test_signatures_are_checked_with_the_release_key(up, tmp_path):
    key = tmp_path / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(key)], check=True)
    allowed = tmp_path / "allowed"
    allowed.write_text("laika-release " + " ".join((tmp_path / "k.pub").read_text().split()[:2]) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"version": "9.9.9"}')
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", "laika-release", str(manifest)], check=True)
    signature = (tmp_path / "manifest.json.sig").read_bytes()
    assert up.verify(manifest.read_bytes(), signature, allowed)
    assert not up.verify(b'{"version": "6.6.6"}', signature, allowed)      # tampered
    assert not up.verify(manifest.read_bytes(), signature, ROOT / "deploy/release-key.pub")  # other key


def test_check_reports_newer_releases(up, monkeypatch, tmp_path):
    monkeypatch.setattr(up, "update_url", lambda: "https://example.invalid/r/")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    monkeypatch.setattr(up, "HOME", tmp_path)
    monkeypatch.setattr(up, "current_version", lambda home=tmp_path: (home / "VERSION").read_text().strip())
    files = {"manifest.json": json.dumps({"version": "1.0.1", "tarball": "laika-1.0.1.tar.gz", "notes": "fixes"}).encode(),
             "manifest.json.sig": b"sig"}
    getter = lambda url: files[url.rsplit("/", 1)[1]]
    r = MemoryRedis()
    info = up.check(r, getter, lambda m, s: True)
    assert info["newer"] and info["latest"] == "1.0.1" and json.loads(r.get(up.AVAILABLE_KEY))["notes"] == "fixes"
    with pytest.raises(ValueError, match="not signed"):
        up.check(r, getter, lambda m, s: False)
    files["manifest.json"] = json.dumps({"version": "1.0.1", "tarball": "../../etc/passwd"}).encode()
    with pytest.raises(ValueError, match="unexpected file"):
        up.check(r, getter, lambda m, s: True)
    monkeypatch.setattr(up, "update_url", lambda: "")
    assert up.check(r) == {"configured": False}


def _tarball(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, body in entries:
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def test_releases_unpack_safely(up, tmp_path):
    good = _tarball([("laika-1.0.1/install.sh", b"#!/bin/bash\n"), ("laika-1.0.1/VERSION", b"1.0.1\n")])
    assert (up.safe_extract(good, tmp_path / "a") / "VERSION").read_text() == "1.0.1\n"
    with pytest.raises(ValueError, match="unsafe"):
        up.safe_extract(_tarball([("../evil", b"x")]), tmp_path / "b")
    with pytest.raises(ValueError, match="does not look like LAIka"):
        up.safe_extract(_tarball([("other/readme", b"x")]), tmp_path / "c")


def test_a_git_checkout_is_never_replaced(up, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(up, "HOME", tmp_path)
    with pytest.raises(SystemExit, match="git checkout"):
        up.apply(MemoryRedis())
