"""Internet access for project tests, on request.

Project gates (tests) run without network by default. When tests fail in a
way that looks like they needed the internet, or the builder says so with a
line "NEEDS_NETWORK: <reason>", the worker records a request on the builder
job; the orchestrator then hands the job to the operator (needs_human,
kind "network") instead of retrying blindly. The operator answers with
scripts/job-review.py network JOB once|always|deny:
  once   tests of this change may use the internet (job field network_allowed)
  always tests of this project may use the internet (project gate_network)
  deny   keep tests offline; the builder is told to make them work offline
and the job resumes the step that was stopped (a rebuild or a repair).
"""

import re

PATTERNS = re.compile(
    r"getaddrinfo|ENOTFOUND|EAI_AGAIN|ENETUNREACH|Temporary failure in name resolution|"
    r"Name or service not known|Could not resolve host|Network is unreachable|"
    r"nodename nor servname|NameResolutionError|gaierror|Failed to establish a new connection|"
    r"No address associated with hostname|dial tcp: lookup|Could not resolve hostname|"
    r"unable to access 'https?://|Failed to connect to [^ ]+ port|ERR_NAME_NOT_RESOLVED",
    re.IGNORECASE,
)
AGENT_LINE = re.compile(r"^\s*NEEDS_NETWORK:\s*(.+)$", re.MULTILINE)
REQUEST_FIELDS = ("network_request", "network_request_reason", "network_request_step", "network_request_at")
CHOICES = ("once", "always", "deny")

PROMPT_NOTE = (
    "- Tests run WITHOUT internet access. If they genuinely need it (e.g. they call an "
    "external API that cannot be mocked), keep them and end your final message with one "
    "line: NEEDS_NETWORK: <short reason>. The operator decides.\n"
)
DENIED_NOTE = (
    "The operator decided tests must run without internet access: make them work offline "
    "(mock or stub external services) instead of reaching the network."
)


def allowed(project_fields, builder):
    """Tests of this builder's change may use the network."""
    return (project_fields or {}).get("gate_network") == "always" or (builder or {}).get("network_allowed") == "1"


CREDENTIALS = re.compile(r"(//)[^/\s:@]+:[^/\s@]+@")


def evidence(output, limit=6):
    """The log lines that show a network failure (empty if none), with
    credentials in URLs masked: they are shown on the dashboard."""
    lines = [CREDENTIALS.sub(r"\1***@", line.strip())[:240]
             for line in (output or "").splitlines() if PATTERNS.search(line)]
    return lines[:limit]


def agent_reason(text):
    found = AGENT_LINE.findall(text or "")
    return found[-1].strip()[:400] if found else ""


def request(builder, gate_output, step, agent_text="", project_fields=None):
    """Fields to record on the builder when a failed gate should become a
    network request, or None (network already allowed, operator said no,
    or nothing points at the network)."""
    if allowed(project_fields, builder) or (builder or {}).get("network_denied") == "1":
        return None
    reason = agent_reason(agent_text)
    lines = evidence(gate_output)
    if not reason and not lines:
        return None
    detail = (f"The builder says: {reason}\n" if reason else "") + "\n".join(lines)
    return {"network_request": "pending", "network_request_reason": detail.strip()[:2000],
            "network_request_step": step}
