"""services/provider_health.py and services/redaction.py: why a provider
could not serve a call, when to try again, and secrets kept out of errors."""

import datetime
import json

import pytest

from laika_testing import MemoryRedis, ROOT, load_module

health = load_module(ROOT / "services/provider_health.py", "provider_health_test")
redaction = load_module(ROOT / "services/redaction.py", "redaction_test")
NOW = datetime.datetime(2026, 10, 18, 22, 0, tzinfo=datetime.timezone.utc).timestamp()


@pytest.mark.parametrize("text,status,kind", [
    ("You've hit your usage limit. Upgrade to Pro or try again in 2 hours 5 minutes.", None, "quota"),
    ("Claude AI usage limit reached|1792000000", None, "quota"),
    ("You've hit your session limit · resets 3am (America/Chicago)", None, "quota"),
    ("Your credit balance is too low to access the Anthropic API.", None, "quota"),
    ("stream error: exceeded retry limit, last status: 429 Too Many Requests", None, "rate"),
    ("anything", 429, "rate"),
    ("Invalid API key · Please run /login", None, "auth"),
    ("unexpected status 401 Unauthorized", None, "auth"),
    ("OAuth token has expired", None, "auth"),
    ("Overloaded", None, "unavailable"),
    ("anything", 529, "unavailable"),
    ("SyntaxError in tests/test_app.py: unexpected indent", None, "failure"),
    ("Please log in to the admin page in the app you are building", None, "failure"),   # the task, not the CLI
    ("", None, "failure"),
])
def test_classification_uses_only_explicit_provider_wording(text, status, kind):
    assert health.classify(text, status, now=NOW).kind == kind


@pytest.mark.parametrize("text,seconds", [
    ("try again in 2 hours 5 minutes", 2 * 3600 + 300),
    ("Please try again in 45m", 45 * 60),
    ("retry-after: 30", 30),
    ("usage limit reached|%d" % (NOW + 3600), 3600),
])
def test_reset_times(text, seconds):
    until, hint = health.reset_time(text, now=NOW)
    assert hint == "parsed" and abs(until - (NOW + seconds)) < 1


def test_clock_and_dated_reset_times_with_a_zone():
    # 22:00 UTC = 17:00 in Chicago; "resets 3am" is the next morning there.
    until, hint = health.reset_time("You've hit your limit · resets 3am (America/Chicago)", now=NOW)
    assert hint == "parsed" and until == datetime.datetime(2026, 10, 19, 8, 0, tzinfo=datetime.timezone.utc).timestamp()
    until, _ = health.reset_time("try again on Oct 21st 3:48 AM (UTC)", now=NOW)
    assert until == datetime.datetime(2026, 10, 21, 3, 48, tzinfo=datetime.timezone.utc).timestamp()
    assert health.reset_time("usage limit reached|%d" % (NOW - 60), now=NOW) == (None, "stale")
    assert health.reset_time("no time here", now=NOW) == (None, "absent")


def test_holds_use_the_reset_time_plus_a_margin_and_are_capped():
    r = MemoryRedis()
    seconds = health.hold(r, "codex", health.classify("You've hit your usage limit. try again in 2h", now=NOW), now=NOW)
    assert seconds == 7200 + health.MARGIN and health.held(r, "codex")
    detail = health.info(r, "codex", now=NOW)
    assert detail["kind"] == "quota" and detail["until"] == NOW + seconds
    week = health.classify("usage limit reached|%d" % (NOW + 30 * 86400), now=NOW)
    assert health.hold_seconds(week, now=NOW) == health.CAP_SECONDS["quota"]           # capped at 8 days
    assert health.hold_seconds(health.classify("429 Too Many Requests", now=NOW), now=NOW) == health.DEFAULT_SECONDS["rate"]
    assert health.hold_seconds(health.classify("a failing test", now=NOW), now=NOW) == 0
    short = health.Failure("rate", "x", until=NOW + 10)
    assert health.hold(r, "codex", short, now=NOW) == seconds                         # never shortened
    health.release(r, "codex")
    assert not health.held(r, "codex")


def test_codex_failures_come_only_from_its_final_error_event():
    log = "\n".join(json.dumps(e) for e in [
        {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "429 Too Many Requests"}},
        {"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again in 3h."}},
    ])
    failure = health.codex_failure(log, now=NOW)
    assert failure.kind == "quota" and abs(failure.until - (NOW + 3 * 3600)) < 1
    ok_log = "\n".join(json.dumps(e) for e in [
        {"type": "error", "message": "stream error: 429 Too Many Requests; retrying"},
        {"type": "turn.completed", "usage": {}},
    ])
    assert health.codex_failure(ok_log, now=NOW) is None      # it recovered and finished
    assert health.codex_failure('{"type":"item.completed"}', now=NOW) is None


def test_secrets_are_masked(monkeypatch):
    monkeypatch.setenv("REDIS_PASSWORD", "s3cret-redis-pass-123")
    text = ("redis://default:s3cret-redis-pass-123@127.0.0.1:6379 failed; "
            "Authorization: Bearer abcdefghijklmnop1234; token=abcdef123456; "
            "key sk-ant-api03-AAAAAAAAAAAAAAAAAAAA ghp_" + "a" * 36)  # release-scan: allow (fakes)
    clean = redaction.redact(text)
    for secret in ("s3cret-redis-pass-123", "abcdefghijklmnop1234", "abcdef123456", "sk-ant-api03", "ghp_aaaa"):
        assert secret not in clean
    assert "redis://default:[redacted]@127.0.0.1" in clean
    assert redaction.redact("\x1b[31mplain\x1b[0m text") == "plain text"


def test_a_codex_sign_in_it_cannot_use_is_a_sign_in_problem():
    log = '{"type":"turn.failed","error":{"message":"plan type is required for chatgpt authentication"}}\n'
    assert health.codex_failure(log).kind == "auth"
