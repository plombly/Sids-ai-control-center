#!/bin/bash
# Clean-machine test of install.sh: a fresh distro in a throwaway systemd
# container (its own Docker inside), then checks, reinstall and uninstall.
#
#   tests/install/clean-install.sh IMAGE [SOURCE]
#   IMAGE:  ubuntu:22.04 | ubuntu:24.04 | debian:12 | fedora:42 ...
#   SOURCE: the commit to install (default HEAD of this repository)
#   RELEASE_DIR=folder: instead, install the signed release in that folder
#   (manifest.json, .sig, tarball) the way `curl … | sudo bash` does, from
#   a web server inside the machine.
#   PLATFORM=linux/amd64/v2: that image variant (RHEL 10 clones on CPUs
#   without x86-64-v3, e.g. quay.io/almalinuxorg/almalinux:10).
#
# Needs Docker and internet access (packages, images). Leaves nothing
# behind: the container and its volume are removed at the end (KEEP=1 keeps
# the container for a look inside). Exit 0 only if every step passed.
set -uo pipefail

IMAGE=${1:?image, e.g. ubuntu:24.04}
REF=${2:-HEAD}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
NAME="laika-install-test-$(echo "$IMAGE" | tr -c 'a-z0-9' '-' | sed 's/-*$//')"
TAG="laika-install-base:$(echo "$IMAGE" | tr ':/' '--')"
WORK=$(mktemp -d)
LOG="${LOG_DIR:-$WORK}/$NAME.log"
failures=0

step() { printf '\n=== %s\n' "$*" | tee -a "$LOG"; }
pass() { printf 'PASS %s\n' "$*" | tee -a "$LOG"; }
bad() { printf 'FAIL %s\n' "$*" | tee -a "$LOG"; failures=$((failures + 1)); }
in_box() { docker exec "$NAME" bash -lc "$*" >>"$LOG" 2>&1; }
out_box() { docker exec "$NAME" bash -lc "$*" 2>>"$LOG"; }
cleanup() {
  if [ "${KEEP:-0}" = 1 ]; then echo "kept container $NAME"; else
    docker rm -f -v "$NAME" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

step "base image $IMAGE with systemd"
case "$IMAGE" in
  # firewalld on, as on a real Fedora / RHEL server: the installer must open LAIka's ports.
  fedora*|rocky*|alma*|*/rockylinux*|*/almalinux*) PKG="dnf -y -q install systemd procps-ng iproute sudo firewalld && systemctl enable firewalld && dnf clean all" ;;
  *) PKG="apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q systemd systemd-sysv dbus iproute2 sudo ca-certificates curl && rm -rf /var/lib/apt/lists/*" ;;
esac
cat > "$WORK/Dockerfile" <<EOF
FROM $IMAGE
RUN $PKG
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
EOF
docker build -q ${PLATFORM:+--platform "$PLATFORM"} -t "$TAG" "$WORK" >>"$LOG" 2>&1 || { bad "base image"; exit 1; }

step "start a fresh machine"
docker rm -f -v "$NAME" >/dev/null 2>&1
docker run -d ${PLATFORM:+--platform "$PLATFORM"} --name "$NAME" --hostname laika-test --privileged --cgroupns=private \
  --tmpfs /run --tmpfs /run/lock -v /var/lib/docker -v /var/lib/containerd --memory=4g --cpus=2 "$TAG" >>"$LOG" 2>&1 \
  || { bad "container start"; exit 1; }
for _ in $(seq 1 30); do
  state=$(out_box "systemctl is-system-running" || true)
  case "$state" in running|degraded) break ;; esac
  sleep 2
done
pass "systemd is up ($state)"

step "copy LAIka ($REF) into the machine"
git -C "$REPO" archive --format=tar --prefix=laika-src/ "$REF" > "$WORK/src.tar"
docker cp "$WORK/src.tar" "$NAME:/root/src.tar" >>"$LOG" 2>&1
in_box "tar -xf /root/src.tar -C /root" \
  || true

step "install"
started=$(date +%s)
how="--source /root/laika-src"
if [ -n "${RELEASE_DIR:-}" ]; then
  mkdir -p "$WORK/releases/latest/download"
  cp "$RELEASE_DIR"/* "$WORK/releases/latest/download/"
  docker cp "$WORK/releases" "$NAME:/root/releases" >>"$LOG" 2>&1
  in_box "command -v python3 >/dev/null || (apt-get update -q >/dev/null 2>&1; apt-get install -y -q python3 >/dev/null 2>&1 || dnf -y -q install python3 >/dev/null 2>&1); cd /root && nohup python3 -m http.server 8099 --bind 127.0.0.1 >/dev/null 2>&1 &"
  sleep 2
  how=""  # no --source: download, verify and install the signed release
fi
if in_box "apt-get update -q >/dev/null 2>&1 || true; command -v git >/dev/null || (apt-get install -y -q git >/dev/null 2>&1 || dnf -y -q install git >/dev/null 2>&1); LAIKA_RELEASE_URL=http://127.0.0.1:8099/releases bash /root/laika-src/install.sh $how --yes"; then
  pass "install.sh finished in $(( $(date +%s) - started ))s"
else
  bad "install.sh failed (see $LOG)"; KEEP=${KEEP:-0}; exit 1
fi

if [ -n "${RELEASE_DIR:-}" ]; then
  out_box "test ! -e /opt/laika/.git && test -f /opt/laika/VERSION && test ! -e /opt/laika/CLAUDE.md" \
    && pass "installed the release $(out_box 'cat /opt/laika/VERSION') (no git, no development files)" || bad "not a release install"
fi

step "doctor"
doctor=$(out_box "laika doctor --json" || true)
echo "$doctor" >> "$LOG"
fails=$(echo "$doctor" | python3 -c 'import json,sys; print(" ".join(c["name"] for c in json.load(sys.stdin) if c["level"] == "fail"))' 2>/dev/null || echo "unreadable")
[ -z "$fails" ] && pass "doctor: no failures" || bad "doctor failures: $fails"

if out_box "command -v firewall-cmd" >/dev/null; then
  ports=$(out_box 'firewall-cmd --zone="$(firewall-cmd --get-default-zone)" --list-ports')
  case " $ports " in *" 8080/tcp "*"8100-8299/tcp"*|*"8100-8299/tcp"*" 8080/tcp "*) pass "firewalld opened 8080 and 8100-8299 ($ports)" ;;
    *) bad "firewalld ports: ${ports:-none}" ;; esac
fi

step "first-run setup through the API (setup code -> admin -> sign in)"
code=$(out_box "/var/lib/laika/venv/bin/python /opt/laika/scripts/laika-admin.py setup-code" | tail -1)
api() { out_box "curl -s -m 10 -c /root/jar -b /root/jar -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:8080' $*"; }
created=$(api "-X POST http://127.0.0.1:8080/api/setup/admin -d '{\"code\": \"$code\", \"username\": \"tester\", \"password\": \"correct horse battery\"}'")
echo "$created" | grep -q '"user"' && pass "administrator created" || bad "administrator: $created"
api "-X POST http://127.0.0.1:8080/api/auth/login -d '{\"username\": \"tester\", \"password\": \"correct horse battery\"}'" >/dev/null
state=$(api "http://127.0.0.1:8080/api/auth/state")
echo "$state" | grep -q '"signed_in":true' && pass "signed in" || bad "sign-in: $state"
projects=$(api "http://127.0.0.1:8080/api/projects")
echo "$projects" | grep -q '"laika"' && bad "production install shows the built-in project" || pass "no built-in project (production profile)"

step "a project created through the dashboard, worked on by the laika user"
api "-X POST http://127.0.0.1:8080/api/projects -d '{\"id\": \"demo\", \"name\": \"Demo\", \"source\": \"empty\", \"request_id\": \"install-test-0001\"}'" >> "$LOG"
for _ in $(seq 1 30); do
  out_box "test -d /var/lib/laika/projects/demo/repo/.git" && break
  sleep 2
done
if out_box "test -d /var/lib/laika/projects/demo/repo/.git"; then
  pass "project repository created by the operator service"
  shared=$(out_box "git -C /var/lib/laika/projects/demo/repo config core.sharedRepository")
  case "$shared" in group|1|true) pass "repository shared with the laika group ($shared)" ;; *) bad "core.sharedRepository=$shared" ;; esac
  if in_box "cd /tmp && setpriv --reuid=laika --regid=laika --init-groups env HOME=/var/lib/laika/home GIT_CONFIG_GLOBAL=/etc/laika/gitconfig sh -c 'umask 0007; cd /var/lib/laika/projects/demo/repo && git worktree add -q -b t /var/lib/laika/worktrees/job-test && cd /var/lib/laika/worktrees/job-test && echo hi > f && git add f && git -c user.name=t -c user.email=t@t commit -qm t'"; then
    pass "the laika user can branch and commit in it"
  else
    bad "the laika user cannot work in the repository"
  fi
else
  bad "project was not created"
fi
sandbox=$(out_box "setpriv --reuid=laika --regid=laika --init-groups bwrap --die-with-parent --unshare-all --cap-drop ALL --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --tmpfs /etc/laika sh -c 'cat /etc/laika/redis.env 2>&1; id -un'" || true)
echo "$sandbox" | grep -q 'REDIS_PASSWORD' && bad "sandbox can read LAIka's secrets" || pass "sandbox runs as laika without LAIka's secrets ($sandbox)"
out_box "ps -o user= -C python | sort | uniq -c" >> "$LOG"
workers_as=$(out_box "ps -eo user=,args= | grep '[w]orker/worker.py' | awk '{print \$1}' | sort -u | tr '\n' ' '")
[ "$(echo $workers_as)" = laika ] && pass "workers run as laika" || bad "workers run as: ${workers_as:-none}"

step "teams: an invited member sees only their project"
made=$(api "-X POST http://127.0.0.1:8080/api/users -d '{\"username\": \"Sam\", \"access\": {\"demo\": \"view\"}}'")
token=$(echo "$made" | python3 -c 'import json,sys; print(json.load(sys.stdin)["invite"]["token"])' 2>/dev/null || true)
member() { out_box "curl -s -m 10 -c /root/jar2 -b /root/jar2 -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:8080' $*"; }
if [ -n "$token" ]; then
  member "-X POST http://127.0.0.1:8080/api/invites/$token -d '{\"password\": \"another long password\"}'" | grep -q '"signed_in":true' \
    && pass "invite accepted" || bad "invite not accepted"
  seen=$(member "http://127.0.0.1:8080/api/projects" | python3 -c 'import json,sys; print(sorted(p["id"] for p in json.load(sys.stdin)))' 2>/dev/null)
  [ "$seen" = "['demo']" ] && pass "member sees only demo" || bad "member sees: $seen"
  code=$(out_box "curl -s -o /dev/null -w '%{http_code}' -b /root/jar2 http://127.0.0.1:8080/api/settings")
  [ "$code" = 403 ] && pass "member cannot open settings" || bad "member settings: $code"
  code=$(out_box "curl -s -o /dev/null -w '%{http_code}' -b /root/jar2 -X POST -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:8080' http://127.0.0.1:8080/api/projects/demo/builds")
  [ "$code" = 403 ] && pass "a viewer cannot build" || bad "viewer build: $code"
else
  bad "could not add a user: $made"
fi

step "repair is harmless"
in_box "laika repair --yes" && pass "laika repair" || bad "laika repair"
api "http://127.0.0.1:8080/api/auth/state" | grep -q '"signed_in":true' && pass "still signed in after repair (secrets kept)" || bad "repair changed secrets"

step "uninstall keeping data, then install again"
in_box "laika uninstall --yes" && pass "uninstall" || bad "uninstall"
out_box "test ! -e /opt/laika && test -d /var/lib/laika/projects/demo && ! systemctl list-units --plain --no-legend 'laika-*' | grep -q ." \
  && pass "program removed, data kept" || bad "uninstall left the wrong things"
in_box "LAIKA_RELEASE_URL=http://127.0.0.1:8099/releases bash /root/laika-src/install.sh $how --yes" && pass "reinstall" || bad "reinstall"
state=$(api "-X POST http://127.0.0.1:8080/api/auth/login -d '{\"username\": \"tester\", \"password\": \"correct horse battery\"}'")
echo "$state" | grep -q '"user"' && pass "the old administrator still signs in" || bad "after reinstall: $state"

if out_box "command -v firewall-cmd" >/dev/null; then
  ports=$(out_box 'firewall-cmd --zone="$(firewall-cmd --get-default-zone)" --list-ports')
  case "$ports" in *8080*) pass "ports open again after reinstall" ;; *) bad "ports after reinstall: ${ports:-none}" ;; esac
fi

step "uninstall --purge"
in_box "laika uninstall --purge --yes" && pass "purge" || bad "purge"
out_box "test ! -e /var/lib/laika && test ! -e /etc/laika && ! id laika" && pass "nothing left" || bad "purge left files or the user"

printf '\n%s: %s (%d failures). Log: %s\n' "$IMAGE" "$([ $failures = 0 ] && echo PASSED || echo FAILED)" "$failures" "$LOG"
[ "${LOG_DIR:-}" ] || cp "$LOG" "/tmp/$NAME.log" 2>/dev/null
exit $((failures > 0))
