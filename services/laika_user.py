"""The dedicated `laika` user (v1.0).

Agents, tests, project apps and the goal assistant run as this user; a few
control services stay root because they start units, drive Docker or set
firewall rules (laika-operator, laika-apps, laika-scaler, backups, the
watchdog). Both share LAIka's data through the `laika` group: directories
are setgid laika, every program runs with umask 0007, and repositories use
core.sharedRepository=group, so files either side creates stay usable by
the other and nothing is readable by other accounts.

When the user does not exist (an older install, tests) everything runs as
the current user, exactly as before.
"""

import os
import pwd

USER = os.environ.get("LAIKA_USER", "laika")
UMASK = "0007"


def account(name=None):
    """pwd entry of the laika user, or None."""
    try:
        return pwd.getpwnam(name or USER)
    except KeyError:
        return None


def _switch():
    """The account to switch to: only when running as root and it exists."""
    if os.geteuid() != 0:
        return None
    return account()


def systemd_run_args():
    """systemd-run options that start a transient unit as the laika user."""
    user = _switch()
    if user is None:
        return []
    return [f"--uid={user.pw_uid}", f"--gid={user.pw_gid}", f"--property=UMask={UMASK}",
            f"--setenv=HOME={user.pw_dir}", "--setenv=DISABLE_AUTOUPDATER=1"]


def command_prefix():
    """argv prefix that runs a command as the laika user (from root)."""
    user = _switch()
    if user is None:
        return []
    return ["setpriv", f"--reuid={user.pw_uid}", f"--regid={user.pw_gid}", "--init-groups",
            "env", f"HOME={user.pw_dir}", "DISABLE_AUTOUPDATER=1"]
