#!/usr/bin/env python3
"""AI provider sign-in for the web setup and Settings (host side).

    laika-providers.py status          publish whether Claude and Codex are
                                        signed in (laika:providers:status)
    laika-providers.py login codex     Codex device sign-in: publishes the
                                        link and code to show in the browser
    laika-providers.py login claude    Claude subscription sign-in: publishes
                                        the link, waits for the code the
                                        browser sends back, finishes sign-in
    laika-providers.py apply-keys      sign Codex in with the OpenAI API key
                                        saved from the web (providers.env)

Progress of a sign-in: laika:provider-login:<provider> (JSON: state
starting|waiting|done|failed, url, code, message). Email addresses and
tokens are never published.
"""

import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
import laika_env  # noqa: E402,F401

PROVIDERS_ENV = Path(os.environ.get("LAIKA_PROVIDERS_DIR", "/etc/laika/providers")) / "providers.env"
LOGIN_TIMEOUT = 900
URL = re.compile(r"https://[^\s\"'<>]+")
CODE = re.compile(r"\b[A-Z0-9]{4,5}-[A-Z0-9]{4,5}\b")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def redis_client():
    import redis as redis_lib
    import laika_redis
    return redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                    password=laika_redis.password(), decode_responses=True)


def publish(r, provider, **fields):
    r.set(f"laika:provider-login:{provider}", json.dumps({"at": time.time(), **fields}), ex=3600)


def run(argv, timeout=30, stdin=None):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def claude_status(runner=run):
    result = runner(["claude", "auth", "status", "--json"])
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return {"installed": result.returncode != 127, "signed_in": False}
    return {"installed": True, "signed_in": bool(data.get("loggedIn")), "method": data.get("authMethod") or "",
            "plan": data.get("subscriptionType") or ""}


def codex_status(runner=run):
    result = runner(["codex", "login", "status"])
    text = (result.stdout + result.stderr).strip()
    if result.returncode == 127:
        return {"installed": False, "signed_in": False}
    signed = result.returncode == 0 and text.lower().startswith("logged in")
    return {"installed": True, "signed_in": signed, "method": text.replace("Logged in using ", "") if signed else ""}


def status(r, runner=run):
    report = {"claude": claude_status(runner), "codex": codex_status(runner), "checked_at": time.time()}
    r.set("laika:providers:status", json.dumps(report))
    return report


def parse_prompt(text):
    """(url, code) found in a CLI's sign-in output so far."""
    clean = ANSI.sub("", text)
    url = next(iter(URL.findall(clean)), "")
    code = next(iter(CODE.findall(clean)), "")
    return url, code


def login_codex(r, clock=time.time):
    publish(r, "codex", state="starting")
    process = subprocess.Popen(["codex", "login", "--device-auth"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)
    output, shown, deadline = "", False, clock() + LOGIN_TIMEOUT
    while process.poll() is None and clock() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], 1)
        if ready:
            output += process.stdout.readline()
            url, code = parse_prompt(output)
            if url and code and not shown:
                publish(r, "codex", state="waiting", url=url, code=code,
                        message="Open the link, sign in to ChatGPT and enter the code.")
                shown = True
    if process.poll() is None:
        process.kill()
        publish(r, "codex", state="failed", message="Sign-in timed out")
        return 1
    ok = process.returncode == 0
    publish(r, "codex", state="done" if ok else "failed", message="" if ok else ANSI.sub("", output)[-300:])
    status(r)
    return 0 if ok else 1


def login_claude(r, clock=time.time):
    import pty
    publish(r, "claude", state="starting")
    r.delete("laika:provider-login:claude:code")
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("claude", ["claude", "auth", "login", "--claudeai"])
    output, shown, sent, deadline = "", False, False, clock() + LOGIN_TIMEOUT
    while clock() < deadline:
        ready, _, _ = select.select([fd], [], [], 1)
        if ready:
            try:
                output += os.read(fd, 4096).decode(errors="replace")
            except OSError:
                break
            url, _ = parse_prompt(output)
            if url and not shown:
                publish(r, "claude", state="waiting", url=url,
                        message="Open the link, sign in to Claude, then paste the code it shows you.")
                shown = True
        if shown and not sent:
            code = r.get("laika:provider-login:claude:code")
            if code:
                os.write(fd, (code.strip() + "\r").encode())
                r.delete("laika:provider-login:claude:code")
                sent = True
                publish(r, "claude", state="checking", message="Checking the code…")
        finished, code_status = os.waitpid(pid, os.WNOHANG)
        if finished:
            ok = os.waitstatus_to_exitcode(code_status) == 0
            publish(r, "claude", state="done" if ok else "failed",
                    message="" if ok else ANSI.sub("", output)[-300:])
            status(r)
            return 0 if ok else 1
    os.kill(pid, 9)
    publish(r, "claude", state="failed", message="Sign-in timed out")
    return 1


def read_env(path=PROVIDERS_ENV):
    values = {}
    try:
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.startswith("#"):
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def apply_keys(r, runner=run):
    # The unit gets providers.env as its environment (the file itself is root-only).
    key = os.environ.get("OPENAI_API_KEY") or read_env().get("OPENAI_API_KEY", "")
    if key:
        result = runner(["codex", "login", "--with-api-key"], stdin=key + "\n")
        print("codex:", "signed in with the API key" if result.returncode == 0 else "failed")
    status(r, runner)
    return 0


def main(argv):
    r = redis_client()
    action = argv[1] if len(argv) > 1 else ""
    if action == "status":
        print(json.dumps(status(r)))
        return 0
    if action == "login" and len(argv) > 2 and argv[2] in ("codex", "claude"):
        return login_codex(r) if argv[2] == "codex" else login_claude(r)
    if action == "apply-keys":
        return apply_keys(r)
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
