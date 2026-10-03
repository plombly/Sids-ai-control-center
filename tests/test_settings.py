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
    assert "user: alex" in out and r.records["laika:auth:admin"]["password"].startswith("scrypt$")
    assert "laika:sessions:s1" not in r.records
    assert admin.main(["x", "setup-code"], r) == 1  # an administrator exists


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


def test_a_claude_sign_in_is_judged_by_the_cli_afterwards(monkeypatch):
    helper = load_module(ROOT / "scripts/laika-providers.py", "laika_providers_finish_test")
    r = MemoryRedis()
    monkeypatch.setattr(helper, "status", lambda r, runner=None: {})
    assert helper.finish_claude(r, 0, "", check=lambda: {"signed_in": True}) == 0
    assert json.loads(r.get("laika:provider-login:claude"))["state"] == "done"
    # No token printed and the CLI failed: a failure, even if an older login exists.
    assert helper.finish_claude(r, 1, "", check=lambda: {"signed_in": True}) == 1
    assert helper.finish_claude(r, 1, "\x1b[1mInvalid code\x1b[0m", check=lambda: {"signed_in": False}) == 1
    failed = json.loads(r.get("laika:provider-login:claude"))
    assert failed["state"] == "failed" and failed["message"] == "Invalid code"


def test_host_tools_share_files_with_the_laika_group_but_keep_stricter_masks():
    import os
    env = load_module(ROOT / "services/laika_env.py", "laika_env_umask_test")
    before = os.umask(0o022)
    try:
        env.shared_umask()
        assert os.umask(0o077) == 0o007
        env.shared_umask()
        assert os.umask(0o022) == 0o077
    finally:
        os.umask(before)


def test_claude_sign_in_saves_the_long_lived_token_and_never_publishes_it(tmp_path, monkeypatch):
    helper = load_module(ROOT / "scripts/laika-providers.py", "laika_providers_token_test")
    r = MemoryRedis()
    r.values["laika:provider-cooldown:claude"] = "claude unavailable"
    monkeypatch.setattr(helper, "status", lambda r, runner=None: {})
    token = "sk-ant-oat01-" + "A1b2_C3-d4" * 9
    output = ("Paste code here if prompted >\r\n\x1b[1C\x1b[32m✓\x1b[39m\x1b[1CLong-lived\x1b[1Ctoken:\r\n"
              + token + "\r\nStore\x1b[1Cthis\x1b[1Ctoken\r\n")
    saved = []
    assert helper.finish_claude(r, 0, output, saver=saved.append) == 0
    assert saved == [token]
    published = r.get("laika:provider-login:claude")
    assert json.loads(published)["state"] == "done" and token not in published
    assert r.get("laika:provider-cooldown:claude") is None
    path = tmp_path / ".config/laika/claude-token"
    helper.save_token(token, path)
    assert path.read_text().strip() == token and oct(path.stat().st_mode)[-3:] == "600"
    # A failure message never carries a token either.
    assert helper.finish_claude(r, 1, "bad " + token + " nope", check=lambda: {"signed_in": False}, saver=saved.append) == 0
    saved.clear()
    assert helper.finish_claude(r, 1, "\x1b[31mInvalid code\x1b[39m", check=lambda: {"signed_in": False}, saver=saved.append) == 1
    assert json.loads(r.get("laika:provider-login:claude"))["message"] == "Invalid code" and saved == []


def test_claude_runs_use_the_saved_token(tmp_path, monkeypatch):
    agent_cli = load_module(ROOT / "services/agent_cli.py", "agent_cli_token_test")
    assert agent_cli.claude_token(tmp_path) == ""
    (tmp_path / ".config/laika").mkdir(parents=True)
    (tmp_path / ".config/laika/claude-token").write_text("not a token\n")
    assert agent_cli.claude_token(tmp_path) == ""
    token = "sk-ant-oat01-" + "x" * 40
    (tmp_path / ".config/laika/claude-token").write_text(token + "\n")
    assert agent_cli.claude_token(tmp_path) == token
