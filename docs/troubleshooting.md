# Troubleshooting

Start with:

```sh
sudo laika doctor
```

It names what is wrong and how to fix it. The most common problems:

## The dashboard does not open

- Use `http://<server-address>:8080` from a machine on the same network
  (or through your VPN).
- `sudo laika doctor`: are the containers and the dashboard healthy?
- A firewall on the server must allow port 8080 from your network.

## "Enter the setup code" but I don't have one

`sudo laika setup-code` prints a new one (valid 24 hours). It only works
until the administrator account exists.

## I forgot my password

`sudo laika reset-password` prints a new one and signs every browser out.

## Jobs stay queued

- **No worker running?** The worker panel shows them; `sudo laika doctor`
  checks the scaler. With automatic scaling, a new worker comes after jobs
  have waited a few minutes.
- **New work paused (memory)?** The worker panel says so; free memory or
  add more.
- **Jobs waiting for each other** or for the same files run one after
  another on purpose.

## Jobs fail at once with sign-in errors

The AI tools are not signed in for the `laika` user: **Settings → AI &
pipeline → Accounts**. If Claude hit a usage limit, LAIka uses Codex for
30 minutes on its own.

## Tests fail with network errors

Tests run offline. LAIka asks under **Needs you** whether to allow internet
for that change or the whole project; dependencies should be installed by
the project's setup command (which has internet).

## An app keeps crashing

The project's Overview shows the app's last log lines. Check its run
command and port (it must listen on `$PORT`, on `0.0.0.0`), and its memory
limit in Settings.

## "Request from another site refused"

The dashboard was opened through an address the server doesn't see as its
own (for example through a proxy that changes the host name). Open it
directly, or make the proxy pass `X-Forwarded-Host`.

## Disk is filling up

Builds, worktrees and logs are cleaned automatically; backups are kept for
14 days by default (Settings → Backups). `sudo du -sh /var/lib/laika/*
/var/backups/laika` shows where space goes.

## Getting help

Run `sudo laika doctor --json` and include its output (it contains no
secrets) when you ask for help.
