"""Settings: the validated schema (apps/api/settings_schema.py) and how host
programs apply stored values (services/laika_env.py)."""

import json
import os
import sys

import pytest

from laika_testing import ROOT, MemoryRedis, load_module

sys.path.insert(0, str(ROOT / "apps/api"))
import settings_schema as schema  # noqa: E402


def test_every_field_has_a_valid_default_and_a_known_section():
    sections = {s for s, _, _ in schema.SECTIONS}
    for field in schema.FIELDS:
        assert field["section"] in sections, field["key"]
        assert schema.clean(field, field["default"]) == field["default"] or field["type"] in ("int", "float"), field["key"]
        assert field["apply"] in ("live", "restart", "host")
    assert len({f["key"] for f in schema.FIELDS}) == len(schema.FIELDS)


@pytest.mark.parametrize("key,raw,ok", [
    ("MAX_REPAIR_ATTEMPTS", "3", "3"), ("MAX_REPAIR_ATTEMPTS", 99, None), ("MAX_REPAIR_ATTEMPTS", "x", None),
    ("THEME", "light", "light"), ("THEME", "pink", None), ("ACCENT", "#AABBCC", "#aabbcc"), ("ACCENT", "red", None),
    ("REDUCE_MOTION", True, "true"), ("REDUCE_MOTION", "maybe", None), ("BACKUP_TIME", "04:05", "04:05"),
    ("BACKUP_TIME", "25:00", None), ("HOME_SECTIONS", "recent,needs", "recent,needs"),
    ("HOME_SECTIONS", "needs,needs", None), ("DEFAULT_MODEL", "gpt-5.6-luna", "gpt-5.6-luna"),
    ("DEFAULT_MODEL", "x; rm -rf /", None), ("SERVER_NAME", "  Home  ", "Home"), ("SERVER_NAME", "", None),
    ("BACKUP_REMOTE", "", ""), ("BACKUP_REMOTE", "nas:/backups bad", None), ("BUILD_MEMORY", "8g", "8g"),
])
def test_values_are_validated(key, raw, ok):
    cleaned, errors = schema.validate({key: raw})
    if ok is None:
        assert key in errors and key not in cleaned
    else:
        assert cleaned[key] == ok and not errors


def test_cross_field_rules_and_unknown_keys():
    assert "SUPPORT_WORKERS" in schema.validate({"WORKER_COUNT": 2, "SUPPORT_WORKERS": 2})[1]
    assert "APPS_PORT_MAX" in schema.validate({"APPS_PORT_MIN": 9000, "APPS_PORT_MAX": 8000})[1]
    assert schema.validate({"NOPE": 1})[1] == {"NOPE": "unknown setting"}


def test_save_keeps_only_changes_and_reports_what_needs_applying():
    r = MemoryRedis()
    cleaned, _ = schema.validate({"THEME": "light", "MAX_REPAIR_ATTEMPTS": 3, "WORKER_COUNT": 4})
    assert schema.save(r, cleaned) == ["MAX_REPAIR_ATTEMPTS"]  # worker settings are read live by the scaler
    assert schema.values(r)["THEME"] == "light" and schema.values(r)["CLOCK"] == "24h"
    schema.save(r, {"THEME": "system"})  # back to the default: no longer stored
    assert "THEME" not in json.loads(r.get(schema.KEY))


def test_environment_maps_service_settings_and_providers():
    r = MemoryRedis()
    schema.save(r, schema.validate({"PROVIDER_BUILDER": "claude", "PROVIDER_REVIEWER": "codex", "MAX_BUILD_ATTEMPTS": 3,
                                    "THEME": "light"})[0])
    env = schema.environment(r)
    assert env["MAX_BUILD_ATTEMPTS"] == "3" and "THEME" not in env
    assert env["ROLE_PROVIDERS"] == "builder=claude,reviewer=codex"


def test_programs_apply_stored_settings_unless_told_not_to(monkeypatch):
    monkeypatch.setenv("LAIKA_SETTINGS_SOURCE", "none")
    module = load_module(ROOT / "services/laika_env.py")
    assert module.APPLIED == {}


def test_support_workers_are_the_last_ones():
    worker = load_module(ROOT / "services/worker/worker.py")
    env = {"WORKER_COUNT": "8", "SUPPORT_WORKERS": "2"}
    assert [worker.worker_class(f"laika-worker-{n:02d}", env) for n in (1, 6, 7, 8)] == ["general", "general", "support", "support"]
    assert worker.worker_class("laika-worker-03", {"WORKER_COUNT": "4", "SUPPORT_WORKERS": "0"}) == "general"
    assert worker.worker_class("laika-worker-01", {"WORKER_CLASS": "support"}) == "support"


def test_system_apply_writes_the_backup_schedule(tmp_path, monkeypatch):
    module = load_module(ROOT / "scripts/laika-system.py")
    assert not hasattr(module, "workers")  # the scaler owns worker units
    monkeypatch.setattr(module, "run", lambda *argv, check=False: None)
    monkeypatch.setattr(module, "UNIT_DIR", tmp_path)
    monkeypatch.setenv("BACKUP_TIME", "04:45")
    module.timers()
    assert "OnCalendar=*-*-* 04:45:00" in (tmp_path / "laika-backup.timer.d/schedule.conf").read_text()


def test_setup_code_and_password_reset(capsys):
    import hashlib
    admin = load_module(ROOT / "scripts/laika-admin.py")
    r = MemoryRedis()
    assert admin.main(["x", "setup-code"], r) == 0
    code = capsys.readouterr().out.strip()
    assert len(code) == 9 and code[4] == "-"
    assert r.get("laika:setup:code") == hashlib.sha256(code.replace("-", "").encode()).hexdigest()
    assert admin.main(["x", "reset-password"], r) == 1  # no administrator yet
    r.records["laika:auth:admin"] = {"username": "alex", "password": "old"}
    r.values["laika:session-ids"] = {"s1"}
    r.records["laika:sessions:s1"] = {"user": "alex"}
    assert admin.main(["x", "reset-password"], r) == 0
    out = capsys.readouterr().out
    # The 1.0 administrator became the first user account.
    assert "user: alex" in out and r.records["laika:users:alex"]["password"].startswith("scrypt$")
    assert r.records["laika:users:alex"]["role"] == "admin" and "laika:auth:admin" not in r.records
    assert "laika:sessions:s1" not in r.records
    assert admin.main(["x", "setup-code"], r) == 1  # an administrator exists
    assert admin.main(["x", "reset-password", "nobody"], r) == 1
    assert admin.main(["x", "reset-password", "ALEX"], r) == 0  # any capitals


def test_provider_helper_reads_cli_output_without_leaking_details():
    import subprocess
    helper = load_module(ROOT / "scripts/laika-providers.py")
    out = "\x1b[1mFollow these steps\x1b[0m\n1. Open https://auth.openai.com/codex/device in your browser\n2. Enter this one-time code ABCD-12345\n"
    assert helper.parse_prompt(out) == ("https://auth.openai.com/codex/device", "ABCD-12345")
    assert helper.parse_prompt("nothing yet") == ("", "")
    fake = lambda argv, **k: subprocess.CompletedProcess(argv, 0, '{"loggedIn": true, "authMethod": "claude.ai", "email": "me@x", "subscriptionType": "pro"}', "")
    assert helper.claude_status(fake) == {"installed": True, "signed_in": True, "method": "claude.ai", "plan": "pro"}
    codex = lambda argv, **k: subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT\n", "")
    assert helper.codex_status(codex) == {"installed": True, "signed_in": True, "method": "ChatGPT"}
    missing = lambda argv, **k: subprocess.CompletedProcess(argv, 127, "", "not found")
    assert helper.codex_status(missing)["installed"] is False


def test_claude_sign_in_saves_a_working_token_and_never_shows_one(tmp_path, monkeypatch):
    helper = load_module(ROOT / "scripts/laika-providers.py", "laika_providers_token_test")
    monkeypatch.setattr(helper, "SIGNIN_LOG", tmp_path / "signin.log")
    monkeypatch.setattr(helper, "status", lambda r, runner=None: {})
    r = MemoryRedis()
    r.values["laika:provider-cooldown:claude"] = "claude unavailable"
    for token in ("sk-ant-oat01-" + "A1b2_C3-d4" * 9, "sk-ant-at01-" + "Zz9_-" * 18):
        # As the CLI drew it: cursor moves for spaces, colours, odd control codes.
        output = ("\x1b[?25l\x1b[1CSigned\x1b[1Cin!\x1b[1CYour\x1b[1COAuth\x1b[1Ctoken:\r\n" + token
                  + "\r\nStore\x1b[1Cthis\x1b[1Ctoken\x1b[>4m\x1b[<u\x1b(B\x1b7\x1b8\r\n")
        saved, tested = [], []
        assert helper.finish_claude(r, 0, output, saver=saved.append, tester=lambda t: tested.append(t) or True) == 0
        assert saved == [token] and tested == [token]
        stored = json.dumps(r.values) + json.dumps(r.records)
        assert token not in stored and "sk-ant" not in stored
        log = (tmp_path / "signin.log").read_text()
        assert token not in log and "[token]" in log
        assert f"type {token.split('-')[2]}, {len(token)} characters" in log
    assert r.get("laika:provider-cooldown:claude") is None
    # A token Claude does not accept is not saved.
    saved = []
    assert helper.finish_claude(r, 0, "x sk-ant-at01-" + "q" * 40, saver=saved.append, tester=lambda t: False) == 1
    assert saved == [] and "did not accept" in json.loads(r.get("laika:provider-login:claude"))["message"]
    # No token: a plain failure message, never the CLI's own output.
    assert helper.finish_claude(r, 1, "\x1b[31mInvalid code sk-ant-partial\x1b[39m", saver=saved.append) == 1
    message = json.loads(r.get("laika:provider-login:claude"))["message"]
    assert message == helper.FAILED and "sk-ant" not in message
    path = tmp_path / ".config/laika/claude-token"
    helper.save_token("sk-ant-at01-" + "w" * 40, path)
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_claude_runs_use_the_saved_token(tmp_path, monkeypatch):
    agent_cli = load_module(ROOT / "services/agent_cli.py", "agent_cli_token_test")
    assert agent_cli.claude_token(tmp_path) == ""
    (tmp_path / ".config/laika").mkdir(parents=True)
    (tmp_path / ".config/laika/claude-token").write_text("not a token\n")
    assert agent_cli.claude_token(tmp_path) == ""
    for token in ("sk-ant-oat01-" + "x" * 40, "sk-ant-at01-" + "y" * 40):
        (tmp_path / ".config/laika/claude-token").write_text(token + "\n")
        assert agent_cli.claude_token(tmp_path) == token


def test_sign_in_transcripts_never_keep_the_token(tmp_path):
    helper = load_module(ROOT / "scripts/laika-providers.py", "laika_providers_log_test")
    token = "sk-ant-at01-" + "Z" * 50
    helper.keep_transcript("Your\x1b[1Ctoken:\r\n" + token + "\r\n", tmp_path / "log")
    text = (tmp_path / "log").read_text()
    assert token not in text and "[token]" in text and "Your token:" in text


def test_the_token_is_read_from_the_rendered_screen(tmp_path, monkeypatch):
    """The CLI skips cells that already show the right character; only the
    rendered screen has the whole token (the bug: an 'o' went missing)."""
    helper = load_module(ROOT / "scripts/laika-providers.py", "laika_providers_screen_test")
    token = "sk-ant-oat01-" + "Q7w_" * 23
    # Frame 1 leaves "o" in column 8; frame 2 skips over it with a cursor move.
    output = "\x1b[2J\x1b[H       o\r\n" + "\x1b[H" + "sk-ant-\x1b[1C" + token[8:] + "\r\n"
    assert helper.token_candidates(output)[0] == token
    saved = []
    monkeypatch.setattr(helper, "SIGNIN_LOG", tmp_path / "log")
    monkeypatch.setattr(helper, "status", lambda r, runner=None: {})
    assert helper.finish_claude(MemoryRedis(), 0, output, saver=saved.append, tester=lambda t: t == token) == 0
    assert saved == [token]


def test_sign_in_waits_for_a_cli_that_is_updating(monkeypatch):
    helper = load_module(ROOT / "scripts/laika-providers.py", "laika_providers_wait_test")
    import shutil
    seen = iter([None, None, "/usr/local/bin/claude"])
    monkeypatch.setattr(shutil, "which", lambda name: next(seen))
    assert helper.wait_for_cli("claude", sleep=lambda s: None) is True
    monkeypatch.setattr(shutil, "which", lambda name: None)
    times = iter(range(0, 1000, 50))
    assert helper.wait_for_cli("claude", seconds=120, clock=lambda: next(times), sleep=lambda s: None) is False


def test_a_revoked_token_shows_as_not_signed_in():
    import subprocess
    helper = load_module(ROOT / "scripts/laika-providers.py", "laika_providers_revoked_test")
    token = "sk-ant-oat01-" + "r" * 60
    def runner(argv, **kwargs):
        if argv[:2] == ["claude", "-p"]:
            return subprocess.CompletedProcess(argv, 1, json.dumps({"is_error": True, "result": "OAuth token revoked"}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"loggedIn": True}), "")
    info = helper.claude_status(runner, token=token)
    assert info["signed_in"] is False and "sign in again" in info["method"]
    good = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, json.dumps({"is_error": False, "result": "OK", "loggedIn": True}), "")
    assert helper.claude_status(good, token=token)["signed_in"] is True
