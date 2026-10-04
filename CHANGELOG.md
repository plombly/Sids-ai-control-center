# Changelog

All notable changes to LAIka. Versions follow [semantic versioning](https://semver.org).

## 1.2.0

- **Edit files in the browser.** Files → double-click a text file (or
  right-click → Edit, or New file). App data saves at once; code saves as
  one commit on main. A file that changed since you opened it is never
  overwritten: you are told, and keep your text. Tab indents, Enter keeps
  the indentation, Ctrl+S saves, optional line wrap. Files up to 2 MB; View
  access opens them read-only.

- **Fedora and RHEL.** Fedora 41+ and RHEL / AlmaLinux / Rocky Linux 9 and
  10 are supported. Fedora 42, AlmaLinux 9 and Rocky Linux 9 pass the same
  clean-machine test as Ubuntu and Debian; version 10 needs an x86-64-v3
  CPU (checked before installing) and is not yet tested.
  - RHEL 9 gets Python 3.11 from its own packages.
  - Docker's packages and the nftables-based iptables are installed.
  - If firewalld is running, the installer opens 8080 and 8100–8299 (and
    the uninstaller closes them); `laika doctor` checks the ports.
- **Notifications for everyone.** Settings → My notifications: each person
  sets their own Discord webhook or ntfy topic, which events they want,
  quiet hours and a weekly digest.
  - They only hear about projects they can see, at the level each event
    needs: approvals for Approve, stuck jobs and app problems for Build,
    their own goals (or every goal), and backups and health for
    administrators.
  - The server's channel stays, as Settings → Server notifications.
  - Removing a person removes their notification settings.

## 1.1.1

- Settings → AI explains that Claude's and OpenAI's sign-in pages ask you to
  authorize **Claude Code** / **Codex**, the tools LAIka works through.
- Tests run as any user and never touch the server's real data (the public
  repository's checks failed on GitHub because they ran as a normal user).
- The updater unpacks releases with an explicit safe filter (ready for
  Python 3.14).

## 1.1.0

Teams: several people can share one LAIka, and its AI accounts.

- **People and roles.** Settings → Users: administrators add people with a
  one-time invite link (24 hours; each person chooses their own password),
  change their role or access, disable them (signed out at once), send a
  new link to reset a sign-in, or remove them. LAIka always keeps at least
  one administrator. The 1.0 administrator becomes the first account.
- **Per-project access:** View, Build or Approve; access to a parent covers
  its children. Members see only their projects; the dashboard shows only
  what they can use; the server checks every request, and anything not
  explicitly allowed for members is administrator-only.
- **Shared AI accounts:** everyone works with the server's Claude and Codex
  sign-ins; goals record who gave them.
- **Phones** act for the person who paired them.
- `laika reset-password` takes a username.
- Tables on phones keep whole words and scroll sideways.
- Settings → AI shows a revoked or expired Claude token as not signed in
  (it is checked with Claude, not only found on disk).

## 1.0.2

- **Sign-in on phones:** usernames ignore capitals, and phones no longer
  capitalize or autocorrect the username field.

## 1.0.1

Fixes from the first days of real use.

- **Claude sign-in made for servers.** Settings → AI now signs Claude in
  with a long-lived token (one year) instead of the everyday login, whose
  automatic renewal failed on a server where many processes share it. The
  token is checked with one tiny call before it is saved, is readable
  only by the `laika` user, and never appears on a page or in a log. The
  sign-in reads the CLI's screen the way a terminal shows it, shows the
  whole link, and waits out a CLI that is updating itself.
- **Goal assistant** knows how LAIka runs apps (`$PORT` on `0.0.0.0`, the
  server's address instead of `localhost`, one repository per project in
  a group) and no longer asks about them or gets them wrong.
- **Phones and tablets:** pages no longer open zoomed in or scroll
  sideways (Settings → Notifications did), and tapping a field no longer
  zooms in on iPhones.
- **Host tools** run by hand as root keep the files they create usable by
  the `laika` user.

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
