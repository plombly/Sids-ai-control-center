"""Release scan: secrets and private patterns never ship or get pushed."""

import re
import subprocess

import pytest

from laika_testing import ROOT, load_module


@pytest.fixture
def rel():
    return load_module(ROOT / "scripts/laika-release.py", "laika_release_test")


FAKE = {
    "private key": "-----BEGIN OPENSSH PRIVATE KEY-----",  # release-scan: allow (fake)
    "Anthropic key": "key = sk-ant-api03-" + "A" * 30,
    "GitHub token": "ghp_" + "a" * 36,
    "Discord webhook": "https://discord.com/api/webhooks/123456/" + "x" * 30,
    "password in a URL": "git clone https://bob:longpassword@example.com/x.git",  # release-scan: allow (fake)
    "credential assignment": 'password = "' + "Q" * 20 + '"',
}


@pytest.mark.parametrize("label", sorted(FAKE))
def test_each_kind_of_secret_is_found_without_echoing_it(rel, label):
    findings = rel.scan_text("f.py", "fine line\n" + FAKE[label], rel.SECRETS)
    assert findings == [f"f.py:2: {label}"]
    assert FAKE[label] not in " ".join(findings)


def test_allowed_lines_and_ordinary_code_pass(rel):
    text = ('REDIS_URL = "redis://redis:6379/0"\nDATABASE_URL: postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/x\n'
            'TOKEN = "operator-test-token-123456"  # release-scan: allow\nkey_name = "OPENAI_API_KEY"\n')
    assert rel.scan_text("f", text, rel.SECRETS) == []


def test_private_patterns_come_from_outside_the_repository(rel, tmp_path):
    file = tmp_path / "forbidden.txt"
    file.write_text("# mine\nsomeone@example\\.org\n\n")
    patterns = rel.private_patterns(file)
    assert rel.scan_text("README.md", "contact someone@example.org", patterns) == ["README.md:1: private pattern"]
    assert rel.private_patterns(tmp_path / "missing") == []


def test_development_files_never_ship(rel):
    files = rel.shipped("HEAD")
    assert "install.sh" in files and "README.md" in files
    for name in ("CLAUDE.md", "scripts/laika-migrate-user.sh"):
        assert name not in files
    assert not any(re.search(r"-todo\.md$|docs/stress-", f) for f in files)


def test_scan_range_sees_added_lines_and_messages(rel, tmp_path, monkeypatch):
    repo = tmp_path / "r"
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("hello\n")
    run("add", "a.txt")
    run("commit", "-qm", "first")
    (repo / "a.txt").write_text("hello\nghp_" + "b" * 36 + "\n")
    run("commit", "-qam", "second")
    monkeypatch.setattr(rel, "ROOT", repo)
    assert rel.scan_range("HEAD~1..HEAD", rel.SECRETS) == ["a.txt:1: GitHub token"]
    assert rel.scan_range("HEAD~1", rel.SECRETS) == []
