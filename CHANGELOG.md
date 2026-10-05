# Changelog

All notable changes to LAIka. Versions follow [semantic versioning](https://semver.org).

## 1.5.0 (2026-10-05)

- **Spending limits.** A monthly dollar limit per project (project Settings →
  Spending limit) and per person (Settings → Users). At 80% the project's
  approvers (or the person) are notified; at 100% new goals and goal-box
  conversations are refused until next month or a higher limit, or, with
  "Only warn", just notified. Work already running always finishes. The
  Overview shows a notice from 80%, Settings and Users show this month's
  spending. When new goals are paused, the goal box says why. The setting
  Settings → AI & pipeline → Spending limits (also asked during first-run
  setup) decides who sets project limits (project approvers and
  administrators, or administrators only) or turns limits off. Claude on a
  subscription counts what the run would cost on the API; Codex on a
  ChatGPT plan often reports nothing.
- **Approve all for a project group.** A parent project's page offers
  "Approve all N" for every ready change in it and its child projects, from
  any goal, through the merge queue (each re-tested and re-reviewed on the
  latest main). Only projects you may approve; LAIka's own changes never.
- Results of Approve, Reject and Approve all stay in the page banner for
  15 seconds instead of vanishing at the next refresh.
- `laika doctor` recognises Claude signed in with a long-lived token.
- The dashboard's Repository panel shows the version on installs from a
  release (instead of "unknown").

## 1.4.0

- **Conversation proposes the goal.** When the idea is clear or it found the
  cause of a problem, the goal-box Conversation ends its reply with a ready
  goal: **Start this goal** or **Edit first**, no re-explaining.
- **Live app log.** A project's app box has a **Live log**: the running
  app's last 200 lines with times, how often it restarted and how it last
  exited, following new lines as they come.
- **Play-test.** After each deploy of a web app, LAIka opens its page in a
  headless browser and reports errors on the page, failed requests, a blank
  page and a screenshot, on the project page and as an "app looks broken"
  notification. Settings → Project defaults → Play-test web apps.

- **Project templates.** New project → **Start from a template**: Web game,
  Website, API + website, Python web service, Command-line tool or Discord
  bot. Each starts with working code, tests and a run command (apps listen
  on the port LAIka assigns), so the first goal can be about features.
- **Remote access through Tailscale.** Settings → Remote access installs
  Tailscale and connects this server to your Tailscale account with a
  sign-in link; it then lists the addresses of the dashboard, your apps and
  SFTP on your private tailnet, reachable from your devices anywhere. No
  port is opened to the internet: LAIka never enables Tailscale Funnel,
  Serve or SSH, warns if they are on, and `laika doctor` fails on Funnel.
- **Fewer review rounds.** A repair gets every blocking finding as a
  checklist, fixes all of them (and other instances of the same mistake in
  the change), and says what it changed for each; a review reports every
  blocking problem in one round and starts a re-review by ticking off the
  earlier findings. The review still checks the whole change.

- **Phones over Tailscale.** Pairing codes and the app's info carry the
  server's Tailscale addresses; the LAIka App tries them when the first
  address does not answer.
- Fixes from the move to the `laika` user: Flutter projects could not
  install dependencies (the SDK now belongs to the laika user), every
  project build failed with "no main branch yet" (host programs now always
  use LAIka's git settings), and Android builds ran out of memory (the
  build recipes fit Gradle into the build's 4 GB).
- Planning interrupted by a restart is planned again; a goal-box
  Conversation sees the app's state, port, log and recent failures.

## 1.3.1

- Projects can be renamed: Settings → Name. Only the name shown changes;
  the id, folders and app port stay.

## 1.3.0

- **Needs you on every project page.** Changes ready to approve and stuck
  jobs of a project (and its child projects) show at the top of its page,
  with the same Approve / Preview / Reject / Try again buttons as the home
  page.
- **Apps start without a run command.** When a project's run command is
  empty, LAIka runs what its code implies: `npm start`, a Procfile's `web:`
  line, Django's `manage.py`, or a Python server that reads `PORT`. Settings
  shows what it detected; type `off` to never run it. (A finished game used
  to sit there unplayable because nobody had set its run command.)
- - **Problems are diagnosed before they are planned.** A goal that reports a
  bug, error, crash or "why…" makes the planner read the code involved and
  the project's recent failures (app crashes and logs, failed and stuck
  jobs) until it can name the likely cause with evidence. Each job then
  says the cause, the fix and a regression test; if the cause stays
  unclear, the first job is "find and fix the cause" with ranked
  hypotheses. Time limit in Settings → AI (7 minutes by default).
- **Smaller jobs.** The planner keeps every job reviewable in one pass: one
  concern and at most 6 files (tests not counted). A plan with a bigger job
  is sent back once to be split. Big jobs used to bounce between review and
  repair, each review finding something new.
- **Approve all merged nothing on release installs.** The merge queue
  walked every project and stopped at the first one whose folder is not a
  git checkout (LAIka's own, on an install from a release), so no queued
  change was ever merged. Each project's queue now runs on its own, and
  approved changes waiting in the queue say so instead of showing Approve.
- **Planning survives restarts.** A goal whose planning was cut off by a
  restart (an update, `laika repair`, a crash) stayed "planning" forever.
  The orchestrator now plans it again (twice at most, then it says so).
- Job details: **Close** works again (and Esc, and leaving the page).
- Releases no longer show red health for "live tree" (it only applies to
  git checkouts), which also pinged the health notification.
- The dashboard's refresh no longer wipes a text selection, typing or a
  click.

- **Conversation.** The goal box has a third way in: an open conversation
  with Claude about the project. The goal box's three buttons now say what
  they do: **Send as written**, **Questionnaire** (the former "Plan it with
  me": up to three quick questions, then a goal to check) and
  **Conversation**. Discuss changes and additions, ask what
  could be better, and get suggestions you had not thought of; Claude reads
  the project's code to make its answers concrete. **Write the goal** turns
  the conversation into the usual editable brief, and you can keep talking
  after that. Nothing is built until you start a goal. The model (Sonnet by
  default) and the cost cap per reply are in Settings → AI.

- **SFTP.** Edit project files with WinSCP, FileZilla, Cyberduck, VS Code or
  `sftp`, signed in with your LAIka username and password (or your own SSH
  key, Settings → SFTP access). Port 2222.
  - You see a folder per project you can open, each with `code` (main) and
    `data` (the app's data folder). View access is read-only.
  - `data` changes at once. `code` changes collect and become **one
    commit by you** 30 seconds after your last change (Settings → SFTP
    server), or when you disconnect. Until then only you see them.
  - A file someone else changed on main meanwhile is never overwritten:
    your version is kept in App data/.laika-sftp-conflicts and you are
    notified ("SFTP changes could not be applied").
  - SFTP only: no shell, commands or forwarding. It runs as the laika user
    with a locked-down service. Failed sign-ins are limited per address
    like the dashboard's.
  - The installer makes the server key and opens the port in firewalld;
    `laika doctor` checks the service and the port.

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
