"""Read-only copies of the other projects in a group, for agents.

A builder, repair or reviewer working in one member of a project group (and
the goal assistant on a parent) may read the other members' code at their
latest main, e.g. an app's builder reading the API it talks to. The copy is
a `git archive` of main (tracked files only: no history, no .git, nothing
untracked such as secrets) under <consumer root>/reference/<member id>,
which the sandbox shows read-only with the rest of the consumer's project
directory. LAIka itself (a parent, never sandboxed) keeps its copies under
LAIKA_REFERENCE_DIR, which every project sandbox hides. It is refreshed when
the member's main moves.
"""

import os
import shutil
import subprocess
from pathlib import Path

MARKER = ".laika-reference-head"
LAIKA_REFERENCE_DIR = Path(os.environ.get("LAIKA_REFERENCE_DIR", "/var/lib/laika/reference"))


def base_dir(consumer):
    return LAIKA_REFERENCE_DIR if consumer.is_builtin else Path(consumer.root) / "reference"


def _head(project):
    result = subprocess.run(["git", "-C", str(project.repo), "rev-parse", "--verify", "-q",
                             f"refs/heads/{project.default_branch}"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def refresh(consumer, member):
    """The up-to-date copy of member's main for consumer, or None if the
    member has no main yet."""
    head = _head(member)
    if not head:
        return None
    base = base_dir(consumer)
    dest = base / member.id
    marker = dest / MARKER
    if marker.is_file() and not marker.is_symlink() and marker.read_text().strip() == head:
        return dest
    base.mkdir(parents=True, exist_ok=True)
    staging = base / f".{member.id}.new"
    if staging.exists() or staging.is_symlink():
        shutil.rmtree(staging) if staging.is_dir() and not staging.is_symlink() else staging.unlink()
    staging.mkdir()
    archive = subprocess.Popen(["git", "-C", str(member.repo), "archive", "--format=tar", head], stdout=subprocess.PIPE)
    extract = subprocess.run(["tar", "-x", "--no-same-owner", "-C", str(staging)], stdin=archive.stdout,
                             capture_output=True)
    archive.stdout.close()
    if archive.wait() != 0 or extract.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"could not copy {member.id}: {extract.stderr.decode(errors='replace')[:200]}")
    (staging / MARKER).write_text(head + "\n")
    if dest.is_symlink():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    os.replace(staging, dest)
    return dest


def prepare(consumer, members):
    """[(member, path)] for every other member with a main, refreshed."""
    copies = []
    for member in members:
        if member.id == consumer.id:
            continue
        try:
            path = refresh(consumer, member)
        except (OSError, RuntimeError) as exc:
            print(f"[reference] {consumer.id} <- {member.id}: {exc}", flush=True)
            continue
        if path is not None:
            copies.append((member, path))
    return copies


def note(consumer, parent_name, copies):
    """Prompt text that tells an agent about its group and the copies."""
    if not copies:
        return ""
    lines = "\n".join(f"- {member.name} ({member.id}): {path}" for member, path in copies)
    return (f"This project ({consumer.name}) is part of the project group \"{parent_name}\". "
            "Read-only copies of the other members at their latest main:\n"
            f"{lines}\n"
            "Read them when your work must match them (APIs, data formats, names). You cannot "
            "change them; work in another member is a separate job.\n")
