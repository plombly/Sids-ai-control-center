"""Filesystem sandbox (bubblewrap) for everything that runs inside a project.

Projects other than SID are untrusted code: their gate runs the project's own
test command, and their agents (Codex builder, Claude reviewer/repair) run
shell commands chosen by a model. Without a sandbox all of it runs as root on
the host and could read SID's secrets or rewrite another project.

Inside the sandbox a process sees:
  - the host filesystem read-only, with SID's own trees, secrets, backups,
    logs, /root and every other project hidden (empty tmpfs);
  - its own project directory read-only (deploy key masked);
  - the job's worktree read-write (its .git pointer file read-only, and the
    repository's .git read-only, so nothing can plant hooks or config that
    SID's own git commands on the host would later execute);
  - a private, empty /tmp;
  - for agents only, their own CLI state (~/.codex, ~/.claude) read-write;
  - for gates, no network at all (a private loopback): tests cannot reach
    SID's Redis or the internet. SID_SANDBOX_GATE_NETWORK=1 shares the host
    network instead.

The SID project itself is the control plane and is never sandboxed here.
"""

import os
from pathlib import Path

BWRAP = os.environ.get("SID_BWRAP", "/usr/bin/bwrap")

# Hidden from every sandbox (replaced by an empty tmpfs when present).
HIDDEN = (
    "/opt/sids-ai-command-center", "/opt/sid-dev", "/opt/sid-worktrees",
    "/opt/sid-projects", "/etc/sid-ai", "/var/backups", "/var/log/sid-ai",
    "/root", "/home", "/srv", "/mnt", "/media",
)
# Sockets that grant root on the host.
MASKED_FILES = ("/run/docker.sock", "/var/run/docker.sock", "/run/containerd/containerd.sock")
# Agent CLI state, re-exposed read-write inside the hidden /root.
AGENT_STATE = ("/root/.codex", "/root/.claude", "/root/.claude.json")


def enabled():
    return os.environ.get("SID_PROJECT_SANDBOX", "1") != "0"


def _exists(path):
    return os.path.lexists(path)


def command(argv, project, workdir, *, kind, writable=True, extra_ro=()):
    """argv wrapped in bubblewrap for this project (unchanged for SID).

    kind: "gate" (no network, no agent state) or "agent". writable=False
    (the planner) leaves workdir read-only like the rest of the project."""
    if project is None or project.is_sid or not enabled():
        return list(argv)
    if kind not in ("gate", "agent"):
        raise ValueError(f"unknown sandbox kind: {kind}")
    workdir = Path(workdir).resolve()
    project_root = Path(getattr(project, "root", "") or project.repo.parent).resolve()
    repo_git = (project.repo / ".git").resolve()
    args = [BWRAP, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc",
            "--unshare-uts", "--unshare-cgroup-try", "--cap-drop", "ALL",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    if kind == "gate" and os.environ.get("SID_SANDBOX_GATE_NETWORK") != "1":
        args.append("--unshare-net")
    for path in HIDDEN:
        if os.path.isdir(path) and not os.path.islink(path):
            args += ["--tmpfs", path]
    for path in sorted({os.path.realpath(path) for path in MASKED_FILES if _exists(path)}):
        args += ["--ro-bind", "/dev/null", path]
    # The project, read-only; its secrets masked.
    if project_root.is_dir():
        args += ["--ro-bind", str(project_root), str(project_root)]
    for secret in ("deploy_key", "deploy_key.pub"):
        if (project_root / secret).exists():
            args += ["--ro-bind", "/dev/null", str(project_root / secret)]
    # A repository registered from outside the project root.
    for path in {project.repo.resolve(), *(Path(p).resolve() for p in extra_ro)}:
        if path.exists() and not str(path).startswith(str(project_root) + os.sep):
            args += ["--ro-bind", str(path), str(path)]
    # The one writable place: this job's worktree. Its .git pointer and the
    # repository's .git stay read-only.
    if writable:
        args += ["--bind", str(workdir), str(workdir)]
        if (workdir / ".git").exists():
            args += ["--ro-bind", str(workdir / ".git"), str(workdir / ".git")]
    if repo_git.exists():
        args += ["--ro-bind", str(repo_git), str(repo_git)]
    if kind == "agent":
        for path in AGENT_STATE:
            if _exists(path):
                args += ["--bind", path, path]
    args += ["--setenv", "TMPDIR", "/tmp", "--unsetenv", "REDIS_URL", "--chdir", str(workdir), "--"]
    return args + list(argv)
