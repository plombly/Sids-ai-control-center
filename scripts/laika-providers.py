#!/usr/bin/env python3
"""AI provider sign-in for the web setup and Settings (host side).

    laika-providers.py status          publish whether Claude and Codex are
                                        signed in (laika:providers:status)
    laika-providers.py login codex     Codex device sign-in: publishes the
                                        link and code to show in the browser
    laika-providers.py login claude    Claude subscription sign-in (claude
                                        setup-token: a long-lived token made
                                        for servers, one year): publishes the
                                        link, waits for the code the browser
                                        sends back, saves the token where only
                                        the laika user can read it
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
# Terminal control codes: CSI (incl. private <, >, =, ? forms), OSC, charset
# selection and lone escapes.
ANSI = re.compile(r"\x1b\[[<>=?]?[0-9;]*[A-Za-z~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]")


def redis_client():
    import redis as redis_lib
    import laika_redis
    return redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                    password=laika_redis.password(), decode_responses=True)


def publish(r, provider, **fields):
    r.set(f"laika:provider-login:{provider}", json.dumps({"at": time.time(), **fields}), ex=3600)


def run(argv, timeout=30, stdin=None, env=None):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def claude_status(runner=run, token=None):
    import agent_cli
    token = agent_cli.claude_token() if token is None else token
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token} if token else None
    result = runner(["claude", "auth", "status", "--json"], **({"env": env} if env else {}))
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = None
    if data is None and not token:
        return {"installed": result.returncode != 127, "signed_in": False}
    # A saved token can have been revoked or have expired: ask Claude (one
    # tiny call; status is only refreshed when someone looks, at most every
    # 10 minutes).
    works = token_works(token, runner) if token else False
    info = {"installed": result.returncode != 127, "signed_in": works or (not token and bool((data or {}).get("loggedIn"))),
            "method": ("long-lived token" if works else "long-lived token no longer works: sign in again") if token
            else (data or {}).get("authMethod") or "",
            "plan": (data or {}).get("subscriptionType") or ""}
    if token:
        try:
            made = agent_cli.claude_token_file().stat().st_mtime
            info["token_expires"] = made + 365 * 86400
        except OSError:
            pass
    return info


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


def plain(text):
    """Terminal output as text: cursor-forward codes (how the CLI draws
    spaces) become spaces, every other control code goes."""
    return ANSI.sub("", re.sub(r"\x1b\[\d*C", " ", text))


def parse_prompt(text):
    """(url, code) found in a CLI's sign-in output so far."""
    clean = plain(text)
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
    shown = re.sub(r"\bsk-\S+", "[key]", ANY_SECRET.sub("[token]", plain(output)))[-300:]
    publish(r, "codex", state="done" if ok else "failed", message="" if ok else shown)
    status(r)
    return 0 if ok else 1


def wait_for_cli(name, seconds=120, clock=time.time, sleep=time.sleep):
    """The CLI updates itself by replacing its files: for a minute or two it
    may be missing. Wait for it rather than fail."""
    import shutil
    deadline = clock() + seconds
    while not shutil.which(name):
        if clock() > deadline:
            return False
        sleep(3)
    return True


def login_claude(r, clock=time.time):
    import pty
    publish(r, "claude", state="starting")
    r.delete("laika:provider-login:claude:code")
    if not wait_for_cli("claude", clock=clock):
        publish(r, "claude", state="failed", message="The Claude command-line tool is missing. Try again in a few minutes.")
        return 1
    pid, fd = pty.fork()
    if pid == 0:
        # A wide terminal: in 80 columns the sign-in link wraps over several
        # lines and the page could show only its first part.
        import fcntl
        import struct
        import termios
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 2000, 0, 0))
        os.execvp("claude", ["claude", "setup-token"])
    output, shown, sent, deadline = "", False, False, clock() + LOGIN_TIMEOUT
    ended = False  # the CLI closed its terminal: it has finished, one way or the other
    while clock() < deadline and not ended:
        ready, _, _ = select.select([fd], [], [], 1)
        if ready:
            try:
                chunk = os.read(fd, 4096).decode(errors="replace")
            except OSError:
                chunk = ""
            if not chunk:
                ended = True
                break
            output += chunk
            url, _ = parse_prompt(output)
            if url and not shown:
                publish(r, "claude", state="waiting", url=url,
                        message="Open the link, sign in to Claude, then paste the code it shows you.")
                shown = True
        if shown and not sent:
            code = r.get("laika:provider-login:claude:code")
            if code:
                # Typed like a person: the code, then Enter on its own (sent
                # together, the CLI may take Enter as part of a paste).
                os.write(fd, code.strip().encode())
                time.sleep(0.5)
                os.write(fd, b"\r")
                r.delete("laika:provider-login:claude:code")
                sent, sent_at, nudged = True, clock(), False
                publish(r, "claude", state="checking", message="Checking the code…")
        if sent and TOKEN_IN_OUTPUT.search(plain(output)):
            # The token is on screen: done, whether or not the CLI waits for a key.
            try:
                os.kill(pid, 15)
            except OSError:
                pass
            os.waitpid(pid, 0)
            return finish_claude(r, 0, output)
        if sent and not nudged and clock() - sent_at > 8:
            os.write(fd, b"\r")  # one more Enter, in case the first one was swallowed
            nudged = True
        finished, code_status = os.waitpid(pid, os.WNOHANG)
        if finished:
            return finish_claude(r, os.waitstatus_to_exitcode(code_status), output)
    if not ended:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        keep_transcript(output)
        publish(r, "claude", state="failed", message="Sign-in timed out")
        return 1
    # The terminal closed before the exit was seen: wait for it (it is quick).
    _, code_status = os.waitpid(pid, 0)
    return finish_claude(r, os.waitstatus_to_exitcode(code_status), output)


# Any Anthropic credential (seen: sk-ant-oat01-…, sk-ant-at01-…): matched
# to save it, and to make sure none is ever shown or logged.
TOKEN_IN_OUTPUT = re.compile(r"sk-ant-[A-Za-z0-9]+-[A-Za-z0-9_-]{20,}")
ANY_SECRET = re.compile(r"sk-ant-\S*")


def save_token(token, path=None):
    """Only the laika user can read it (0600 in its own home)."""
    import agent_cli
    path = Path(path or agent_cli.claude_token_file())
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".new")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    os.replace(temp, path)


SIGNIN_LOG = Path(os.environ.get("LAIKA_SIGNIN_LOG", "/var/log/laika/claude-signin.log"))


def keep_transcript(output, path=None, notes=()):
    """What the CLI showed, tokens masked, for diagnosing a failed sign-in."""
    try:
        text = ANY_SECRET.sub("[token]", plain(output))[-20000:]
        extra = ANY_SECRET.sub("[token]", "\n".join(notes))
        Path(path or SIGNIN_LOG).write_text(text + ("\n--- notes ---\n" + extra + "\n" if notes else ""))
    except OSError:
        pass


def token_works(token, runner=run, note=None):
    """One tiny Claude call with the token: it really signs in. note(text)
    receives Claude's answer for the transcript (never the token)."""
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token}
    result = runner(["claude", "-p", "Reply with the single word OK.", "--model", "haiku", "--output-format", "json"],
                    timeout=90, env=env)
    try:
        answer = json.loads(result.stdout or "{}")
    except ValueError:
        answer = {"result": (result.stdout + result.stderr)[-300:]}
    if note:
        note(f"test call: exit {result.returncode}, is_error {answer.get('is_error')}, "
             f"status {answer.get('api_error_status')}, {str(answer.get('result', ''))[:200]}")
    return result.returncode == 0 and not answer.get("is_error")


def token_shape(token):
    """Safe to log: the public prefix and the length, never the secret part."""
    kind = re.match(r"sk-ant-([A-Za-z0-9]+)-", token)
    return f"type {kind.group(1) if kind else '?'}, {len(token)} characters"


def rendered(output, columns=2000, rows=400):
    """The text a terminal would show. The CLI redraws by writing only the
    cells that changed since the last frame, so a character already on
    screen in the right place is never sent again: only replaying every
    frame (pyte, a terminal emulator) gives the real text."""
    try:
        import pyte
    except ImportError:
        return ""
    screen = pyte.Screen(columns, rows)
    pyte.Stream(screen).feed(output)
    return "\n".join(line.rstrip() for line in screen.display)


def token_candidates(output):
    """The token as the screen shows it first, then the raw readings."""
    first = TOKEN_IN_OUTPUT.findall(rendered(output))
    readings = {output, plain(output), ANSI.sub("", output), re.sub(r"\x1b\[[0-9;?<>=]*[A-Za-z~]", "", output)}
    rest = sorted({m for text in readings for m in TOKEN_IN_OUTPUT.findall(text)} - set(first), key=len, reverse=True)
    return first + rest


FAILED = "Sign-in did not finish. Try again; details for support are in /var/log/laika/claude-signin.log."


def finish_claude(r, exit_code, output, check=None, saver=save_token, tester=token_works):
    """The CLI printed the long-lived token: check that it works, save it
    (never publish it), end any Claude pause. Messages never carry CLI output
    (it can hold the token); the masked transcript goes to SIGNIN_LOG."""
    notes = []
    found = token_candidates(output)
    if found:
        notes.append("token on screen: " + ", ".join(token_shape(t) for t in found))
        working = next((t for t in found if tester(t, note=notes.append)), None) if tester is token_works \
            else next((t for t in found if tester(t)), None)
        keep_transcript(output, notes=notes)
        if not working:
            publish(r, "claude", state="failed", message="Claude did not accept the new token. Try again.")
            status(r)
            return 1
        saver(working)
        r.delete("laika:provider-cooldown:claude")
        publish(r, "claude", state="done", message="Signed in for a year.")
        status(r)
        return 0
    keep_transcript(output, notes=["no token on screen"])
    publish(r, "claude", state="failed", message=FAILED)
    status(r)
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
