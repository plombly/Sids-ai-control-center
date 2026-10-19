"""Check an AI-written change before it can land: the worker is treated as
an attacker. Standard library only; used by the worker at integration and
again on the host at approval (scripts/job-review.py, the authority).

inspect(git, base, candidate) -> {"refused": [...], "sensitive": [...]}

Refused (the change cannot be integrated or approved):
- a submodule / gitlink: main would point at code nobody reviewed
- a symlink whose target is absolute or leaves the repository: once main is
  checked out, LAIka's own tools (gates, the file browser, builds) follow it
- a path that is not plain: "..", ".git" in any letter case, control
  characters, a leading "/" or "-"
- a candidate that does not descend from the base

Sensitive (allowed, but named to the reviewer and on the approval card,
because a change there can weaken checks or affect tools and agents):
git attributes/modules, hooks, CI, agent instructions (CLAUDE.md,
AGENTS.md, .claude/, .codex/), test configuration (conftest.py,
pytest.ini, tox.ini, setup.cfg, pyproject.toml, package.json, Makefile).
"""

import posixpath
import re

SUBMODULE = "160000"
SYMLINK = "120000"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
SENSITIVE = [re.compile(p, re.IGNORECASE) for p in (
    r"(^|/)\.gitattributes$", r"(^|/)\.gitmodules$", r"(^|/)\.githooks/", r"^scripts/hooks/",
    r"^\.github/", r"^\.gitlab-ci\.yml$", r"^\.circleci/",
    r"(^|/)CLAUDE\.md$", r"(^|/)AGENTS\.md$", r"(^|/)\.claude/", r"(^|/)\.codex/", r"(^|/)\.cursor/",
    r"(^|/)conftest\.py$", r"(^|/)pytest\.ini$", r"(^|/)tox\.ini$", r"(^|/)setup\.cfg$",
    r"(^|/)pyproject\.toml$", r"(^|/)package\.json$", r"(^|/)Makefile$", r"(^|/)\.npmrc$",
    r"^scripts/integration-check\.py$",
)]


def unsafe_path(path):
    """Why a changed path is not a plain repository path, or ""."""
    if not path or path.startswith(("/", "-")):
        return "absolute or option-like path"
    if _CONTROL.search(path):
        return "control character in the path"
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "path with an empty, . or .. part"
    if any(part.lower() == ".git" or part.lower().startswith(".git ") for part in parts):
        return ".git in the path"
    return ""


def escaping_link(path, target):
    """Why a symlink at path pointing at target is refused, or ""."""
    if not target or target.startswith("/"):
        return f"symlink to an absolute path ({target[:80]})"
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        return f"symlink leaving the repository ({target[:80]})"
    if any(part.lower() == ".git" for part in resolved.split("/")):
        return "symlink into .git"
    return ""


def inspect(git, base, candidate):
    """git(*args) -> stdout text (raises on failure), run in the repository."""
    refused, sensitive = [], []
    try:
        git("merge-base", "--is-ancestor", base, candidate)
    except Exception:
        return {"refused": [f"{candidate[:12]} does not build on {base[:12]}"], "sensitive": []}
    raw = git("diff", "--raw", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", base, candidate)
    fields = raw.split("\0")
    index = 0
    while index < len(fields):
        meta = fields[index]
        if not meta.startswith(":"):
            index += 1
            continue
        path = fields[index + 1] if index + 1 < len(fields) else ""
        index += 2
        parts = meta[1:].split()
        if len(parts) < 5:
            continue
        new_mode, new_sha, status = parts[1], parts[3], parts[4]
        why = unsafe_path(path)
        if why:
            refused.append(f"{path!r}: {why}")
            continue
        if status != "D":
            if new_mode == SUBMODULE:
                refused.append(f"{path}: adds a submodule (gitlink)")
                continue
            if new_mode == SYMLINK:
                target = git("cat-file", "-p", new_sha).strip("\n")
                why = escaping_link(path, target)
                if why:
                    refused.append(f"{path}: {why}")
                    continue
        if any(pattern.search(path) for pattern in SENSITIVE):
            sensitive.append(path)
    return {"refused": refused, "sensitive": sorted(set(sensitive))}
