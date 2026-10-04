# Contributing to LAIka

Thank you for helping. LAIka writes code on people's servers, so every
change is held to the same contract LAIka holds its own agents to.

## The rules

1. **Tests with every change.** `python scripts/integration-check.py` runs
   everything (CI runs it too); it must pass.
2. **Never commit secrets.** No keys, tokens, passwords, webhooks or
   personal data, not even in tests: use obviously fake values and put
   `release-scan: allow` on that line. Install the pre-push check once:
   `git config core.hooksPath scripts/hooks`.
3. **Keep the safety contract** in [docs/architecture.md](docs/architecture.md):
   exact-candidate integration and review, human approval, no automatic
   merge. Tests check much of it; if you change such code, change the test
   to check the new correct behaviour, never delete a check.
4. **Small, reviewable pull requests** with a clear description of the
   problem and its cause, not only the fix.
5. LAIka is LAN/VPN-only software. Changes that make it easier to expose
   to the internet will not be accepted.

## Development setup

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r deploy/requirements-dev.lock      # Python 3.11+
LAIKA_PYTHON=.venv/bin/python REPO_ROOT=$PWD LAIKA_GATE_SKIP=local-diagnostic \
  python scripts/integration-check.py
```

Node.js 20+ runs the dashboard tests (`apps/web/*.test.js`). The dashboard
is dependency-free: no build step, no packages. `scripts/laika-docs.py
--write` regenerates the settings reference after changing a setting.

To try a full install, `tests/install/clean-install.sh ubuntu:24.04` runs
the installer in a throwaway container (needs Docker).

To work against a running LAIka without touching a real one,
`sudo scripts/laika-dev.sh create` installs your checkout into its own
systemd container (own Docker, data, users and AI sign-ins; dashboard on
port 9080), and `scripts/laika-dev.sh deploy` updates it with your latest
commit while keeping its data. `status`, `shell`, `stop`, `start` and
`destroy` do what they say.
