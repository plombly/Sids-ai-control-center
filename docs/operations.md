# Operations

## Health

- **The dashboard** shows a status line and, under Details, every health
  check: services, workers, backups, disk, memory, load, network exposure.
- **`sudo laika doctor`** checks the whole installation and prints fixes.
- Notifications (when configured) ping you when health turns red, a backup
  fails, an app goes down, or something needs you.

## Backups

Every day (03:30 by default) LAIka saves, to
`/var/backups/laika/snapshots/<time>/`:

- a git bundle of every project,
- the Redis and Postgres databases,
- every app's data,
- LAIka's settings and secrets.

The newest 14 are kept (Settings → Backups). Backups are readable by root
only. Set an **off-site copy** target (an rsync destination such as a NAS)
to copy each backup off the server; a server backup alone does not survive
the server.

Once a month LAIka **restores the newest backup** into throwaway places to
prove it works, and warns you if it does not.

### Restoring

Restore is manual and deliberate:

1. `sudo laika uninstall` (keeps data) or start from a fresh server with
   LAIka installed.
2. Copy the snapshot folder to the server.
3. Restore the parts you need: `git clone <bundle>` for a project,
   the Redis `dump.rdb` into `/var/lib/laika/db/redis`, `psql` with the
   Postgres dump, the config archive into `/etc/laika`.
4. `sudo laika repair`, then `sudo laika doctor`.

## Logs

| What | Where |
| --- | --- |
| a job | dashboard → job → Log, or `/var/log/laika/jobs/<job>.jsonl` |
| tests of a job | `/var/log/laika/jobs/<job>-tests.log` |
| a service | `laika logs orchestrator` (or `operator`, `apps`, `scaler`, `worker@01`) |
| containers | `cd /opt/laika && sudo docker compose logs api` |

Finished job records and logs older than 30 days are pruned weekly.

## Commands

```text
laika status            services and health at a glance
laika doctor            check the installation, with fixes
laika setup-code        one-time code for the first-run setup
laika reset-password    new administrator password
laika restart [TARGET]  restart safely: all, workers, operator, orchestrator, apps, scaler
laika logs [UNIT]       follow a service's log
laika repair            put back missing pieces (keeps data and secrets)
laika update            install the latest release (backup first)
laika uninstall         remove LAIka (keeps data unless --purge)
laika version           the installed version
```

`laika restart` never interrupts a running job: each worker is paused, finishes
its job, and only then restarts.

## Timers

| Timer | When | What |
| --- | --- | --- |
| `laika-backup` | daily 03:30 | backup |
| `laika-watchdog` | every 2 min | health checks |
| `laika-notify` | every minute | notifications |
| `laika-digest` | hourly | weekly digest when due |
| `laika-prune` | weekly | old records and logs |
| `laika-restore-check` | monthly | proves the newest backup restores |
