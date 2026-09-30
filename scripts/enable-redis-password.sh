#!/bin/bash
# Turn on SID's Redis password (or off again with --disable).
#
# Creates the root-only /etc/sid-ai/redis.env with a random REDIS_PASSWORD,
# recreates the redis and api containers with it (docker-compose env_file),
# and restarts every SID service so it reconnects with the password
# (services/sid_redis.py reads the same file). Refuses while a worker is
# busy, because restarting a worker kills its job.
set -euo pipefail

REPO=${REPO:-/opt/sids-ai-command-center}
ENV_FILE=/etc/sid-ai/redis.env
CLI="$REPO/scripts/sid-redis-cli"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
cd "$REPO"

mapfile -t WORKERS < <(systemctl list-units --plain --no-legend 'sid-ai-worker@*.service' | awk '{print $1}')
UNITS=(sid-ai-operator.service sid-ai-orchestrator.service "${WORKERS[@]}")

busy=""
for unit in "${WORKERS[@]}"; do
  n=${unit#sid-ai-worker@}; n=${n%.service}
  status=$("$CLI" hget "sid:workers:sid-worker-$n" status 2>/dev/null || true)
  [ "$status" = "working" ] && busy="$busy sid-worker-$n"
done
if [ -n "$busy" ]; then
  echo "Workers are busy:$busy. Nothing changed; try again when they are idle." >&2
  exit 1
fi

if [ "${1:-}" = "--disable" ]; then
  rm -f "$ENV_FILE"
  echo "Removed $ENV_FILE; Redis will run without a password."
elif [ ! -s "$ENV_FILE" ]; then
  (umask 077; printf 'REDIS_PASSWORD=%s\n' "$(openssl rand -hex 32)" > "$ENV_FILE")
  echo "Created $ENV_FILE (root only)."
else
  echo "Using the existing $ENV_FILE."
fi
[ -e "$ENV_FILE" ] && chmod 600 "$ENV_FILE"

echo "Stopping SID services..."
systemctl stop "${UNITS[@]}"
echo "Recreating redis, api and web..."
docker compose up -d --force-recreate --build redis api web
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' sid-ai-api 2>/dev/null)" = healthy ] && break
  sleep 2
done
echo "Starting SID services..."
systemctl start "${UNITS[@]}"
sleep 5

echo
echo "Without password: $(docker exec sid-ai-redis redis-cli ping 2>&1)"
echo "With password:    $("$CLI" ping)"
echo "API health:       $(curl -s localhost:8000/health | head -c 90)"
systemctl is-active "${UNITS[@]}" | sort | uniq -c
