"""Live job logs for the dashboard.

Builders, reviewers and repairs write a JSONL event log while they run:
Codex (`--json`) and Claude (`--output-format stream-json`). This reads a
job's log from an offset and turns the raw events into a small, uniform
list the dashboard shows as the run happens: messages, commands with their
exit codes and output, file changes, and the final result. Gate output
(<job>-tests.log) is served the same way as plain text.

The API container sees the logs read-only: LAIka's at /job-logs
(/var/log/laika/jobs) and projects' under /projects (/var/lib/laika/projects).
"""

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

MOUNTS = (
    (Path("/var/log/laika/jobs"), Path(os.environ.get("LAIKA_JOB_LOGS_MOUNT", "/job-logs"))),
    (Path("/var/lib/laika/projects"), Path(os.environ.get("LAIKA_PROJECTS_MOUNT", "/projects"))),
)
CHUNK = 256 * 1024
TEXT_LIMIT = 4000
RUNNING = {"claimed", "running", "testing", "reviewing", "integrating", "repairing"}


def container_path(host_path):
    """The log file as this container sees it, or None if not under a mount."""
    if not host_path:
        return None
    host = Path(host_path)
    for host_base, mounted in MOUNTS:
        try:
            rel = host.relative_to(host_base)
        except ValueError:
            continue
        target = (mounted / rel).resolve()
        if mounted.resolve() in target.parents and target.suffix in (".jsonl", ".json", ".log"):
            return target
    return None


def _clip(text):
    text = str(text or "")
    return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + f"\n… ({len(text) - TEXT_LIMIT} more characters)"


def _claude_content(event):
    out = []
    message = event.get("message") or {}
    for part in message.get("content") or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and part.get("text", "").strip():
            out.append({"kind": "message", "text": _clip(part["text"])})
        elif kind == "tool_use":
            tool = part.get("name") or "tool"
            args = part.get("input") or {}
            detail = args.get("command") or args.get("file_path") or args.get("pattern") or ""
            out.append({"kind": "command", "text": f"{tool}: {detail}".strip(": ")})
        elif kind == "tool_result":
            content = part.get("content")
            if isinstance(content, list):
                content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
            out.append({"kind": "error" if part.get("is_error") else "output", "text": _clip(content)})
    return out


def normalize(event):
    """Uniform display events from one raw Codex or Claude event."""
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    # Codex --json
    if kind in ("item.started", "item.completed"):
        item = event.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message" and kind == "item.completed":
            return [{"kind": "message", "text": _clip(item.get("text"))}]
        if item_type == "command_execution":
            if kind == "item.started":
                return [{"kind": "command", "text": _clip(item.get("command"))}]
            exit_code = item.get("exit_code")
            out = item.get("aggregated_output") or ""
            return [{"kind": "error" if exit_code not in (0, None) else "output",
                     "text": _clip(out) if out else f"(exit {exit_code})", "exit_code": exit_code}]
        if item_type == "file_change" and kind == "item.completed":
            changes = item.get("changes") or []
            return [{"kind": "files", "text": ", ".join(f"{c.get('kind', 'edit')} {c.get('path')}" for c in changes)}]
        return []
    if kind == "turn.completed":
        usage = event.get("usage") or {}
        return [{"kind": "result", "text": f"turn done · {usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out tokens"}]
    if kind in ("error", "turn.failed"):
        return [{"kind": "error", "text": _clip(event.get("message") or (event.get("error") or {}).get("message"))}]
    # Claude stream-json
    if kind in ("assistant", "user"):
        return _claude_content(event)
    if kind == "result":
        cost = event.get("total_cost_usd")
        tail = f" · ${cost:.3f}" if isinstance(cost, (int, float)) else ""
        return [{"kind": "error" if event.get("is_error") else "result",
                 "text": _clip(event.get("result") or event.get("subtype")) + tail}]
    return []


def read_events(path, offset):
    """(events, next_offset) from byte offset; only whole lines are consumed."""
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if offset > size:
        offset = 0  # the log was rewritten (a new attempt)
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(CHUNK)
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    events = []
    for line in data[:end].split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            events.extend(normalize(json.loads(line)))
        except ValueError:
            events.append({"kind": "output", "text": _clip(line.decode(errors="replace"))})
    return events, offset + end + 1


def parts_of(path):
    """Specialist reviews (spec, safety, ...) log to <stem>.<part>.json."""
    if path is None:
        return []
    stem = path.name[: -len(".jsonl")] if path.name.endswith(".jsonl") else path.stem
    return sorted(p.name[len(stem) + 1: -len(".json")] for p in path.parent.glob(f"{stem}.*.json"))


@router.get("/api/jobs/{job_id}/log")
def job_log(job_id: str, after: int = Query(default=0, ge=0), tests: bool = False,
            part: str = Query(default="", pattern=r"^[a-z0-9_-]{0,20}$")):
    import main
    if not job_id.replace("-", "").isalnum() or len(job_id) > 64:
        raise HTTPException(status_code=422, detail="Invalid job id")
    job = main._hash(f"laika:jobs:{job_id}")
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    log = job.get("log") or ""
    if tests:
        log = log[: -len(".jsonl")] + "-tests.log" if log.endswith(".jsonl") else ""
    path = container_path(log)
    running = job.get("status") in RUNNING
    parts = [] if tests else parts_of(path)
    if part:
        if part not in parts:
            raise HTTPException(status_code=404, detail="No such part")
        stem = path.name[: -len(".jsonl")] if path.name.endswith(".jsonl") else path.stem
        path = path.parent / f"{stem}.{part}.json"
    if path is None or not path.exists():
        return {"job_id": job_id, "events": [], "next": after, "running": running, "available": False, "parts": parts}
    if tests:
        text = path.read_bytes()[after:after + CHUNK].decode(errors="replace")
        return {"job_id": job_id, "events": [{"kind": "output", "text": text}] if text else [],
                "next": after + len(text.encode()), "running": running, "available": True}
    events, next_offset = read_events(path, after)
    return {"job_id": job_id, "events": events, "next": next_offset, "running": running, "available": True,
            "parts": parts}
