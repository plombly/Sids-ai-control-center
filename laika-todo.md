# LAIka: features and fixes to add

The persistent work list (not shipped: `*-todo.md` is excluded from
releases). Claude keeps it current: read it at the start of every session
and after every context compaction, tick items off as they ship, and add
new ideas the owner mentions. Hands-on checks for the owner live in
`dylan-todo.md`.

Working rules for this list: build on `dev/claude`, deploy to the
development LAIka (`scripts/laika-dev.sh deploy`, http://10.0.0.59:9080) for
the owner to try, then release and deploy to production when they say so.

## 1.4: in progress (agreed 2026-10-04: "groups 1 and 2")

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

### Checkpoint 3: off-site backups
- [ ] Guided setup in Settings → Backups: another machine over SSH/rsync, a
      NAS, or S3-compatible storage; test connection; encrypted at rest for
      S3; the monthly restore check also tests the off-site copy.

### Checkpoint 4: remote access (Tailscale)
- [ ] Settings → Remote access: install / sign in Tailscale from the
      dashboard (auth key), show the tailnet address for the dashboard and
      apps; doctor checks it; nothing public (VPN-only stays the rule).

### Checkpoint 5: the LAIka phone app (exists: laika-app project, Flutter)
- [ ] Continue the existing app (see dylan-todo.md "LAIka App" and "MAJOR
      (later)"): finish what the owner's device tests find, real push
      notifications, Tailscale as an alternative to the in-app OpenVPN,
      release signing. Built from the host by Claude (view-only project).

## Backlog (not scheduled)

- **Share an app with friends:** a public link to ONE app (e.g. Tank Tumble)
  through a tunnel, on/off switch, optional password; LAIka itself stays
  private. (Asked 2026-10-04, group 3.)
- **Spending limits** per project and per person, with a warning when a
  goal gets expensive. (2026-10-04, group 3.)
- Planner: splits big jobs (done 1.3.0); watch whether 6 files is right.
- Approve-all for whole project groups (all goals of a group at once).
- Fedora/RHEL: test RHEL 10 on an x86-64-v3 machine (needs hardware);
  SELinux enforcing on a real install.
- Doctor: recognise a long-lived Claude token (shows only Codex today).
- The dashboard's repository panel says "unknown" on release installs
  (laika:main-head only exists for git checkouts): show the version instead.

## Done (recent)
- 1.3.1 (2026-10-04): project rename.
- 1.3.0 (2026-10-04): SFTP, goal-box Conversation, diagnose-first planner,
  smaller jobs, Needs you on project pages, release-install fixes.
