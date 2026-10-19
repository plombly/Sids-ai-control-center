"""Git as LAIka runs it on AI-written branches: nothing in the repository or
the environment gets to run code or redirect git. Standard library only.

Every git LAIka runs in a worker or for an approval gets:
- no hooks (core.hooksPath to a path that cannot exist), no fsmonitor
- no ext:: / file:: transports and no submodule recursion
- an environment without git's redirection variables (GIT_DIR,
  GIT_WORK_TREE, GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY, GIT_CONFIG_*,
  GIT_EXEC_PATH, GIT_SSH*, askpass helpers); GIT_CONFIG_GLOBAL (LAIka's own
  shared config) is kept
Diffs shown to reviewers additionally use --no-ext-diff --no-textconv
(DIFF_SAFETY), so no repository-defined diff driver ever runs.
"""

import os

NO_HOOKS = "/dev/null/laika-no-hooks"
HARDENING = (
    "-c", f"core.hooksPath={NO_HOOKS}",
    "-c", "core.fsmonitor=false",
    "-c", "protocol.ext.allow=never",
    "-c", "protocol.file.allow=never",
    "-c", "submodule.recurse=false",
)
DIFF_SAFETY = ("--no-ext-diff", "--no-textconv")
_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
         "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
         "GIT_EXEC_PATH", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_TEMPLATE_DIR",
         "GIT_CEILING_DIRECTORIES", "GIT_NAMESPACE", "GIT_COMMON_DIR", "GIT_EXTERNAL_DIFF", "GIT_PAGER",
         "GIT_EDITOR", "GIT_SEQUENCE_EDITOR")


def command(*args):
    """['git', <hardening>, *args]."""
    return ["git", *HARDENING, *args]


def env(base=None):
    """The environment git runs with: base (default os.environ) minus git's
    redirection variables (and every GIT_CONFIG_KEY_n / _VALUE_n)."""
    source = dict(os.environ if base is None else base)
    return {k: v for k, v in source.items()
            if k not in _DROP and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
