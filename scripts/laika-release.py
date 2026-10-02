#!/usr/bin/env python3
"""Make a LAIka release (maintainers only; never needed on a server).

    laika-release.py check  [--ref REF]       what would ship, and a scan of it
    laika-release.py scan   [--range A..B]    scan commits about to be pushed
    laika-release.py build  --ref TAG [--out DIR] [--key KEY]
                                               tarball + manifest + signature
    laika-release.py export --ref REF --to DIR
                                               a fresh repository (one commit,
                                               no history) to publish

What ships: every tracked file except development-only ones (EXCLUDE).
Nothing ships, and nothing is pushed by the pre-push hook, while the scan
finds something that looks like a secret (private keys, API keys, tokens,
passwords in URLs) or matches the maintainer's own private patterns, kept
OUTSIDE the repository (FORBIDDEN_FILE: one regular expression per line,
e.g. your name, e-mail and home network addresses).

Signing: `ssh-keygen -Y sign` with the release key (ed25519, kept offline
by the maintainer); servers verify with deploy/release-key.pub
(scripts/laika-update.py).
"""

import argparse
import fnmatch
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_FILE = Path(os.environ.get("LAIKA_RELEASE_FORBIDDEN", "/etc/laika/release-forbidden.txt"))
NAMESPACE = "laika-release"
# Development-only files: internal notes, assistant context, one-off tools.
EXCLUDE = ("CLAUDE.md", "*-todo.md", "docs/stress-*.md", "docs/smoke-test.md", "scripts/laika-migrate-user.sh",
           "scripts/laika-release.py", "scripts/hooks/*", ".env.example")
# Things that look like credentials. (A test may need a fake one: put
# "release-scan: allow" on that line.)
SECRETS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")),
    ("Slack token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Discord webhook", re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}")),
    ("password in a URL", re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{6,}@(?!postgres\b|redis\b)")),
    ("credential assignment", re.compile(r"(?i)\b(?:password|passwd|secret|token|api_key)\s*[:=]\s*['\"][A-Za-z0-9/+=_-]{16,}['\"]")),
]
ALLOW = "release-scan: allow"


def git(*args, check=True, input_bytes=None):
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, input=input_bytes)
    if check and result.returncode:
        raise SystemExit(f"git {' '.join(args)}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def shipped(ref):
    files = git("ls-tree", "-r", "--name-only", ref).decode().splitlines()
    return [f for f in files if not any(fnmatch.fnmatch(f, pattern) for pattern in EXCLUDE)]


def private_patterns(path=FORBIDDEN_FILE):
    patterns = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(("private pattern", re.compile(line, re.I)))
    except OSError:
        pass
    return patterns


def scan_text(name, text, patterns):
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW in line:
            continue
        for label, pattern in patterns:
            if pattern.search(line):
                # Never echo the match itself: it may be the secret.
                findings.append(f"{name}:{number}: {label}")
    return findings


def scan_files(ref, files, patterns):
    findings = []
    for name in files:
        findings += scan_text(name, name, patterns)  # file names count too
        blob = git("show", f"{ref}:{name}", check=False)
        if b"\0" in blob[:8000]:
            continue  # binary
        findings += scan_text(name, blob.decode(errors="replace"), patterns)
    return findings


def scan_range(spec, patterns):
    """Lines added by the commits in spec (e.g. origin/main..HEAD), plus their messages."""
    findings = []
    diff = git("log", "-p", "--format=commit %H%n%B", "--no-color", spec).decode(errors="replace")
    current = "?"
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("commit "):
            current = f"commit {line[7:19]}"
        elif line.startswith("+") and not line.startswith("+++"):
            findings += scan_text(current, line[1:], patterns)
        elif current.startswith("commit "):
            findings += scan_text(current, line, patterns)
    return findings


def version_at(ref):
    return git("show", f"{ref}:VERSION").decode().strip()


def archive(ref, files, prefix):
    data = git("archive", "--format=tar", f"--prefix={prefix}/", ref, "--", *files)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=9) as gz, \
            tarfile.open(fileobj=io.BytesIO(data), mode="r:") as source:
        for member in source.getmembers():
            gz.addfile(member, source.extractfile(member) if member.isfile() else None)
    return buffer.getvalue()


def sign(path, key):
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(path)], check=True)
    return Path(str(path) + ".sig")


def all_patterns():
    return SECRETS + private_patterns()


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--ref", default="HEAD")
    scan = sub.add_parser("scan")
    scan.add_argument("--range", default="HEAD")
    build = sub.add_parser("build")
    build.add_argument("--ref", required=True)
    build.add_argument("--out", default="release")
    build.add_argument("--key", default=os.environ.get("LAIKA_RELEASE_KEY", "/root/laika-release-key/release_ed25519"))
    build.add_argument("--notes", default="")
    export = sub.add_parser("export")
    export.add_argument("--ref", default="HEAD")
    export.add_argument("--to", required=True)
    args = parser.parse_args(argv)

    if args.command == "scan":
        findings = scan_range(args.range, all_patterns())
        for item in findings:
            print(item, file=sys.stderr)
        return 1 if findings else 0

    files = shipped(args.ref)
    if not private_patterns():
        print(f"warning: no private patterns in {FORBIDDEN_FILE}; only generic secrets are checked", file=sys.stderr)
    findings = scan_files(args.ref, files, all_patterns())
    if args.command == "check":
        print(f"{len(files)} files would ship at {args.ref} ({version_at(args.ref)})")
        for item in findings:
            print(item)
        return 1 if findings else 0
    if findings:
        print("Refusing: the scan found something:\n  " + "\n  ".join(findings), file=sys.stderr)
        return 1

    version = version_at(args.ref)
    if args.command == "export":
        target = Path(args.to)
        if target.exists() and any(target.iterdir()):
            raise SystemExit(f"{target} is not empty")
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", args.ref, "--", *files)), mode="r:") as tar:
            tar.extractall(target, filter="data") if sys.version_info >= (3, 12) else tar.extractall(target)
        env = {**os.environ, "GIT_AUTHOR_NAME": "LAIka", "GIT_AUTHOR_EMAIL": "release@laika.invalid",
               "GIT_COMMITTER_NAME": "LAIka", "GIT_COMMITTER_EMAIL": "release@laika.invalid"}
        for step in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", f"LAIka {version}"],
                     ["tag", f"v{version}"]):
            subprocess.run(["git", "-C", str(target), *step], check=True, env=env)
        print(f"exported {len(files)} files to {target} as one commit, tagged v{version}")
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    name = f"laika-{version}.tar.gz"
    body = archive(args.ref, files, f"laika-{version}")
    (out / name).write_bytes(body)
    manifest = {"version": version, "tarball": name, "sha256": hashlib.sha256(body).hexdigest(),
                "size": len(body), "released_at": int(time.time()), "notes": args.notes,
                "commit": git("rev-parse", args.ref).decode().strip()}
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n")
    sign(manifest_path, Path(args.key))
    print(f"built {out / name} ({len(body)} bytes), manifest.json and manifest.json.sig")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
