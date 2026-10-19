"""Mask secrets in text LAIka stores or shows (job errors, provider messages,
evidence). Standard library only.

redact(text) replaces:
- the values of this process's secret-named environment variables
  (…TOKEN, …SECRET, …PASSWORD, …KEY, DATABASE_URL, REDIS_URL) — the
  most likely leak: a tool printing its own environment
- bearer / basic credentials, key=value secrets, credentials in URLs
- well-known token shapes (Anthropic, OpenAI, GitHub, AWS, Slack, Google,
  JWTs) and private key blocks
and strips ANSI escape codes. Pattern-based: a novel secret format can
pass, so redacted text is still shown only to people who may see the job.
"""

import os
import re

MASK = "[redacted]"
_SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|DATABASE_URL|REDIS_URL|WEBHOOK)",
                          re.IGNORECASE)
_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL),
    re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{8,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,})"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\blaika_[A-Za-z0-9_-]{20,}"),   # LAIka device keys
    re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+"),
]
_BEARER = re.compile(r"\b(Bearer|Basic|token)\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)
_ASSIGN = re.compile(
    r"(?i)\b([A-Za-z_]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)[A-Za-z_]*)"
    r"(\s*[:=]\s*[\"']?)([^\s\"',;]{6,})")
_URL_CREDENTIALS = re.compile(r"([a-z][a-z0-9+.-]*://)([^\s/:@]+):([^\s/@]+)@", re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _environment_secrets():
    values = []
    for name, value in os.environ.items():
        if _SECRET_NAME.search(name) and value and len(value) >= 8:
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact(text):
    if not text:
        return text or ""
    value = _ANSI.sub("", str(text))
    for secret in _environment_secrets():
        value = value.replace(secret, MASK)
    for pattern in _PATTERNS:
        value = pattern.sub(MASK, value)
    value = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", value)
    value = _URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}{m.group(2)}:{MASK}@", value)
    value = _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", value)
    return value
