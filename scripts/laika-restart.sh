#!/bin/bash
# Restart LAIka's own services safely, then check they came back.
#
#   scripts/laika-restart.sh [--wait SECONDS] TARGET...
#   TARGET: operator | orchestrator | apps | scaler | workers | worker@NN | all
#   (workers / all: only the running workers; the scaler stopped the rest)
#
# Workers are never restarted mid-job: each one is paused first (the
# laika:worker-control key, so it claims nothing new), the script waits until
# it is idle (up to --wait seconds, default 600, then refuses), restarts it
# and un-pauses it (a pause someone else set is left in place). Unit files
# are reloaded first, so edited units take effect. Exit 0 only if every
# restarted unit is active again and reporting.
set -uo pipefail

REPO=${REPO:-/opt/laika}
CLI="$REPO/scripts/laika-redis-cli"
WAIT=600
targets=()
while [ $# -gt 0 ]; do
  case "$1" in
    --wait) WAIT=$2; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) targets+=("$1"); shift ;;
  esac
done
[ ${#targets[@]} -gt 0 ] || { sed -n '2,13p' "$0"; exit 2; }
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }

# Only running workers: the scaler stopped the others on purpose.
all_workers() { systemctl list-units --plain --no-legend --state=active,activating 'laika-worker@*.service' | awk '{print $1}' | sort; }
units=()
for target in "${targets[@]}"; do
  case "$target" in
    operator|orchestrator|apps|scaler) units+=("laika-$target.service") ;;
    workers) mapfile -t found < <(all_workers); units+=("${found[@]}") ;;
    worker@[0-9][0-9]) units+=("laika-$target.service") ;;
    all) mapfile -t found < <(all_workers); units+=(laika-operator.service laika-orchestrator.service laika-apps.service laika-scaler.service "${found[@]}") ;;
    *) echo "unknown target: $target (operator, orchestrator, apps, scaler, workers, worker@NN, all)" >&2; exit 2 ;;
  esac
done
mapfile -t units < <(printf '%s\n' "${units[@]}" | awk '!seen[$0]++')

systemctl daemon-reload

worker_id() { local n=${1#laika-worker@}; echo "laika-worker-${n%.service}"; }
status_of() { "$CLI" hget "laika:workers:$1" status 2>/dev/null; }

failed=0
for unit in "${units[@]}"; do
  if [[ "$unit" == laika-worker@* ]]; then
    id=$(worker_id "$unit")
    paused_by_us=0
    if [ "$("$CLI" get "laika:worker-control:$id" 2>/dev/null)" != "disabled" ]; then
      "$CLI" set "laika:worker-control:$id" disabled >/dev/null
      paused_by_us=1
    fi
    waited=0
    while [ "$(status_of "$id")" = "working" ]; do
      if [ "$waited" -ge "$WAIT" ]; then
        echo "REFUSED $unit: still working after ${WAIT}s; left running (un-paused)" >&2
        [ $paused_by_us = 1 ] && "$CLI" del "laika:worker-control:$id" >/dev/null
        failed=1
        continue 2
      fi
      [ $waited = 0 ] && echo "waiting for $id to finish its job..."
      sleep 5; waited=$((waited + 5))
    done
    systemctl restart "$unit"
    [ $paused_by_us = 1 ] && "$CLI" del "laika:worker-control:$id" >/dev/null
  else
    systemctl restart "$unit"
  fi
done

# Came back? Active, and workers / operator heartbeating again.
sleep 6
for unit in "${units[@]}"; do
  state=$(systemctl is-active "$unit")
  note=""
  if [[ "$unit" == laika-worker@* ]]; then
    note=" heartbeat=$(status_of "$(worker_id "$unit")")"
    [ -z "${note#* heartbeat=}" ] && state="no-heartbeat"
  fi
  printf '%-32s %s%s\n' "$unit" "$state" "$note"
  [ "$state" = active ] || failed=1
done
if [[ " ${units[*]} " == *" laika-operator.service "* ]]; then
  if [ -z "$("$CLI" --scan --pattern 'laika:operator-service:*' 2>/dev/null | head -1)" ]; then
    echo "operator: no heartbeat yet" >&2; failed=1
  fi
fi
health=$(curl -s -m 5 localhost:8000/health | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status"))' 2>/dev/null)
echo "api health: ${health:-unreachable}"
[ "$failed" = 0 ] && echo "OK" || echo "PROBLEMS (see above)"
exit $failed
