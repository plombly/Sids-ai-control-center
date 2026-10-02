# Workers

A **worker** runs one job at a time: building, reviewing, repairing or
integrating a change. More workers means more jobs at once, as long as the
server has the memory and your AI plans have the capacity.

## Automatic scaling (default)

LAIka's scaler checks every 15 seconds:

- **Adds a worker** when jobs that could start right now have waited
  (3 minutes by default) with no idle worker to take them, and the server
  has room. Jobs waiting for another job, or for files another job is
  changing, do not count, so a long chain of dependent jobs never adds
  workers. Claude jobs count only up to the free Claude slots, because
  more workers cannot make Claude run more at once.
- **Removes a worker** after workers have sat idle (15 minutes), then one
  every 5 minutes, down to the minimum.
- **Never stops a job.** A worker being removed finishes its current job
  first.

The **Most workers** setting defaults to a suggestion from your server
(2 per CPU, about 0.9 GB of memory each, at most 16).

## By hand

The **+ / −** buttons on the home page's worker panel and in
**Settings → Workers** add or remove one worker. In automatic mode your
change holds for 10 minutes before the scaler takes over again; with
automatic scaling off they change the fixed number.

## When the server runs short

In both modes:

- free memory below 15% for 30 seconds, or a 5-minute load above
  1.5 × the CPU count for 2 minutes: the newest worker finishes its job and
  stops (one every 2 minutes; no new workers for 10 minutes after);
- free memory below 7%: no worker starts a new job until memory recovers.

Each worker is also capped at 4 GB, so one runaway test cannot take the
whole server down.

All of these limits are in **Settings → Workers**. Every change the scaler
makes is listed with its reason under **Right now** on that page.
