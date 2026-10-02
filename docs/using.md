# Using LAIka

## Projects

A project is one git repository. Create one under **Projects → New**:

- **Empty**: LAIka starts a fresh repository.
- **Clone**: from GitHub or any git URL; private repositories get their own
  deploy key, which the page shows you to add to the repository.

Each project page has tabs: **Overview** (goal box, what's happening, the
app), **Activity**, **Files**, **Builds**, **History** and **Settings**.

**Importance** (high / medium / low) decides which project's work runs
first when workers are busy.

## Goals

Type what you want into a goal box, as you would tell a developer:

- **Send as written** plans it straight away.
- **Plan it with me** first asks up to three questions and writes a precise
  brief for you to check, change or send.

LAIka plans the goal into jobs (one-step goals skip planning). Jobs that
touch different files run in parallel; jobs that depend on each other
wait. You can follow every job live: its log, tests, review and time.

Good goals say *what* and *why*, name anything that must not change, and
say how you will check it. Templates on a project's Overview help.

## Approving

When a change has passed its tests and the independent review, it appears
under **Needs you** on the home page:

- **Approve** merges that exact change into the project's main branch.
- **Preview** (web apps) runs the change on its own port so you can try it
  first.
- **Details** shows the diff, the review findings, tests, tokens and time.
- **Reject** discards it.
- **Approve all N** (when one goal produced several changes) queues every
  change you see; they merge one after another, each re-tested and
  re-reviewed on the latest main.

Nothing ever merges without you. If main moved since a change was checked,
LAIka checks it again automatically before it can be approved.

## When something is stuck

A job that keeps failing its tests or its review is handed to you instead
of looping: **Try again** gives it more attempts, **Give up** rejects it.
If its tests need the internet, LAIka asks: allow it for this change,
always for this project, or keep tests offline.

## Apps and previews

Give a project a **run command** (or let LAIka detect it) and its app runs
on this server from the latest main, on a port in 8100–8199, restarted if
it crashes. Its data lives in `/var/lib/laika/project-data/<project>`
(the **App data** tab under Files). Secrets for the app go in the project's
Settings → Secrets.

## Builds

**Builds → Build now** produces a downloadable artifact (an APK, a
desktop bundle, a static site, a wheel…) from main, in a container. LAIka
picks the recipe from the project's stack; you can override the command,
image and output in Settings. The newest builds are kept.

## Files, history and undo

- **Files** browses the code on main and the app's data; upload, move,
  rename and delete (changes to code become one commit on main).
- **History** lists every change on main; **Undo** reverts one (as a new
  commit, so history is never lost).

## Groups

Related projects (for example an app and its API) can form a group: one
parent with children. A goal given to the parent is planned across all of
them; every agent sees the other members' code read-only. A group shares
importance and test-internet settings, has a **Whole group** activity
view, can **Build the whole group** at once, and appears as one entry in
the weekly digest.

## Notifications and the digest

**Settings → Notifications**: a Discord webhook and/or an ntfy topic, what
to send for each event (ping, post or nothing), quiet hours, and a weekly
digest of what got done.
