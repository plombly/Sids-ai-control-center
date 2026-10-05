# LAIka: features and fixes to add

The persistent work list (not shipped: `*-todo.md` is excluded from
releases). Claude keeps it current: read it at the start of every session
and after every context compaction, tick items off as they ship, and add
new ideas the owner mentions. Hands-on checks for the owner live in
`dylan-todo.md`.

Working rules for this list: build on `dev/claude`, deploy to the
development LAIka (`scripts/laika-dev.sh deploy`, http://10.0.0.59:9080) for
the owner to try, then release and deploy to production when they say so.

## 1.4: released 2026-10-04 (checkpoints 1, 2, 4 and the server side of 5)

Order and checkpoints. Report to the owner at each checkpoint.

### Checkpoint 1: polish from the week's testing
- [x] **Diagnose and fix in one go.** The goal-box Conversation can propose a
      ready goal inside its reply (when it has found a cause or the idea is
      clear); the chat shows it with "Start this goal" / "Edit first", so a
      found cause becomes a goal without re-explaining.
- [x] **App logs on the project page.** The apps service publishes the app
      unit's recent log (journal of laika-app-<id>, last ~200 lines) to Redis;
      the project page shows it live (like job logs), with why it last
      crashed / restarted.
- [x] **Play-test step for web apps.** After a deploy of a project with a web
      app, load the page in a headless browser (Docker Playwright image,
      host network, the app's port), record console errors, failed requests
      and a screenshot; show them on the project page and flag a blank page
      or JS errors (notification "app looks broken").
- [x] **Smarter repair rounds.** A repair gets every open finding as a
      checklist and must address all of them; the re-review states for each
      earlier finding whether it is fixed, and only blocks on new findings
      that are real regressions or blocking-severity. The safety contract
      stays: the review still inspects the whole integrated candidate
      (base..candidate).

### Checkpoint 2: project templates
- [x] "New project" offers templates (web game, website, API + website,
      Discord bot, CLI tool, Python script/service, …) that start with a
      working skeleton: tests, a run command that listens on $PORT /
      0.0.0.0, README. The first goal can then be about features.

### Checkpoint 4: remote access (Tailscale)
- [x] Settings → Remote access: install / sign in Tailscale from the
      dashboard (auth key), show the tailnet address for the dashboard and
      apps; doctor checks it; nothing public (VPN-only stays the rule).

### Checkpoint 5: the LAIka phone app (exists: laika-app project, Flutter)
- [x] Server: pairing codes and /api/app/info carry the Tailscale addresses
      (alt_urls / tailnet_urls).
- [x] App: try the other addresses when the first does not answer (merged
      2026-10-04, job b7aa7c2a, reviewed and approved by Claude).
- [x] Android APK rebuilt: build apk9 on the real LAIka (LAIka App -> Builds).
      Owner's device tests: dylan-todo.md "LAIka App".
- [ ] Needs the owner's decision/accounts: real push notifications (FCM/
      APNs need a Firebase project / Apple account; alternative without
      accounts: the per-person ntfy notifications of 1.2 + the ntfy app),
      Android release signing key (kept like the release key), iOS VPN
      (paid Apple account).

## 1.5: in progress (agreed 2026-10-04)

- [x] **Spending limits**: monthly $ per project (project Settings, admin or
      Approve) and per person (Settings -> Users, admin); 80% warning
      notification; at 100% new goals and Conversations are refused
      (default; "warn only" switch per limit); running work finishes.
      Spend = job/planner/assistant cost_usd this calendar month (Claude on a
      subscription reports API-equivalent cost; Codex on ChatGPT often $0).
- [x] **Approve-all for project groups**: on a parent project's page,
      "Approve all N" across the parent and its children (all goals), through
      the merge queue; only projects the person may approve; never LAIka.
- [x] Small: doctor recognises the long-lived Claude token.
- [x] Small: dashboard Repository panel shows the version on release installs.
- [x] Small: dev LAIka tidy-up (Play-test demo, Starter Game, Tailscale).
- [x] Tested 2026-10-04 on laika-dev (API, browser desktop+phone, real
      Conversation turn, two group approve-alls merged). Fixed: approve-all gave
      no feedback on project pages, action results vanished from the banner on
      refresh, months now UTC (API container vs host), read-only limit for
      builders, notifications use project names.
- [x] Owner's follow-ups: SPENDING_LIMITS setting (approvers/admins/off,
      first-run setup + Settings), paused goal box with the reason.
- [ ] Ship 1.5.0 when the owner says so.

## Backlog (not scheduled)

- **Off-site backups** (moved from 1.4 on 2026-10-04: the owner first spins
  up another VM to back up to). Guided setup in Settings → Backups: another
  machine over SSH/rsync, a NAS, or S3-compatible storage; test connection;
  encrypted at rest for S3; the monthly restore check also tests the
  off-site copy.

- **Share an app with friends:** a public link to ONE app (e.g. Tank Tumble)
  through a tunnel, on/off switch, optional password; LAIka itself stays
  private. (Asked 2026-10-04, group 3.)
- Planner: splits big jobs (done 1.3.0); watch whether 6 files is right.
- Fedora/RHEL: test RHEL 10 on an x86-64-v3 machine (needs hardware);
  SELinux enforcing on a real install.

## Done (recent)
- 1.4.0 (2026-10-04): see CHANGELOG.md; production runs it.
- 1.3.1 (2026-10-04): project rename.
- 1.3.0 (2026-10-04): SFTP, goal-box Conversation, diagnose-first planner,
  smaller jobs, Needs you on project pages, release-install fixes.
