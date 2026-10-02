# Changelog

All notable changes to LAIka. Versions follow [semantic versioning](https://semver.org).

## 1.0.0

The first public release.

- **Pipeline**: goals planned into parallel jobs; builders in isolated git
  worktrees; every change integrated onto the latest main, tested, and
  independently reviewed (specification and safety) on the exact
  integrated result; bounded automatic repairs; human approval for every
  merge, one at a time or all of a goal through the merge queue.
- **Projects**: empty or cloned repositories, type and stack detection,
  apps run from main, previews of pending changes, builds of downloadable
  artifacts, files, history with undo, activity, importance, groups of
  related projects planned and approved together.
- **Goal assistant**: a few questions, then a precise brief.
- **AI providers**: Claude and Codex per role, subscription sign-in from
  the browser or API keys, automatic fallback on usage limits, spending
  caps per run.
- **Workers**: automatic scaling on parallel work, idle scale-down,
  memory and load protection, + / − by hand.
- **Security**: sign-in with a one-time setup code, sessions, audit log;
  agents, tests and apps run as an unprivileged user in sandboxes without
  LAIka's secrets; tests offline unless allowed; builds isolated from the
  LAN; per-phone revocable keys.
- **Operations**: one-command installer for Ubuntu and Debian, web setup
  guide, `laika doctor`, repair, uninstall, daily backups with monthly
  restore checks, health watchdog, Discord / ntfy notifications, weekly
  digest, one-click signed updates with automatic rollback.
- **Dashboard**: desktop, tablet and phone; light and dark themes,
  accent colours, density and text size; live job logs, usage and costs.
