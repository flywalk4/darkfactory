#!/bin/sh
# Host-side self-update: put the newest origin/main on this server, check that the factory is healthy, roll
# back otherwise. It runs from a systemd timer outside the containers, so a broken factory is still rolled back.
#
#   sh scripts/self-update.sh            # one round (the timer runs it every 5 minutes)
#
# Status: /data/system/self-update.json inside the data volume (shown on the factory page).
# A commit that failed its health check is remembered in bad-shas and is not installed again; the next commit
# on main is. Only fast-forwards are taken: local edits or a rewritten main stop the update with an error.
set -eu
cd "$(dirname "$0")/.."
# The merge below rewrites this very file while sh is still reading it: run from a private copy.
if [ -z "${DARKFACTORY_UPDATE_COPY:-}" ]; then
    copy=$(mktemp)
    cp "$0" "$copy"
    DARKFACTORY_UPDATE_COPY=1 DARKFACTORY_UPDATE_ROOT=$(pwd) exec sh "$copy"
fi
cd "$DARKFACTORY_UPDATE_ROOT"
trap 'rm -f "$0"' EXIT

BRANCH=${DARKFACTORY_UPDATE_BRANCH:-main}
HEALTH_SECONDS=${DARKFACTORY_UPDATE_HEALTH_SECONDS:-600}
VOLUME=${DATA_VOLUME:-darkfactory_data}
SYSTEM="$(docker volume inspect -f '{{.Mountpoint}}' "$VOLUME")/system"
mkdir -p "$SYSTEM"
STATUS="$SYSTEM/self-update.json"
BAD="$SYSTEM/self-update-bad-shas"
LOG="$SYSTEM/self-update.log"
touch "$BAD"
PORT=$(sed -n 's/^PORT=//p' .env | tail -1)
ADDRESS=$(sed -n 's/^BIND_ADDRESS=//p' .env | tail -1)
URL="http://${ADDRESS:-127.0.0.1}:${PORT:-8080}/"

status() {
    # state, sha, previous, message
    python3 - "$STATUS" "$1" "$2" "$3" "$4" <<'EOF'
import json, sys, time
path, state, sha, previous, message = sys.argv[1:]
json.dump({"state": state, "sha": sha, "previous": previous, "message": message[-2000:], "at": time.time()},
          open(path + ".tmp", "w"))
import os
os.replace(path + ".tmp", path)
EOF
}

deploy() {
    docker compose build -q app executor-image >>"$LOG" 2>&1 &&
        docker compose up -d --no-deps app >>"$LOG" 2>&1 &&
        sh scripts/deploy-worker.sh >>"$LOG" 2>&1
}

healthy() {
    # The app answers and the new worker's scheduler ticks without failures for the whole window.
    started=$1
    deadline=$(($(date +%s) + HEALTH_SECONDS))
    good=0
    while [ "$(date +%s)" -lt "$deadline" ]; do
        sleep 15
        beat=$(cat "$SYSTEM/worker-heartbeat" 2>/dev/null || echo 0)
        now=$(date +%s)
        worker=$(docker compose ps -q worker | tail -1)
        if curl -fsS -o /dev/null --max-time 10 "$URL" &&
            [ "$beat" -ge "$started" ] && [ $((now - beat)) -lt 60 ] &&
            [ -n "$worker" ] &&
            ! docker logs --since "@$started" "$worker" 2>&1 | grep -q "Scheduler tick failed"; then
            good=$((good + 1))
            # Two minutes of a live, error-free scheduler.
            [ "$good" -ge 8 ] && return 0
        else
            good=0
        fi
    done
    return 1
}

git fetch -q origin "$BRANCH"
current=$(git rev-parse HEAD)
target=$(git rev-parse "origin/$BRANCH")
[ "$current" = "$target" ] && exit 0
if grep -qx "$target" "$BAD"; then
    exit 0
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ] || ! git merge-base --is-ancestor "$current" "$target"; then
    status failed "$target" "$current" "Not a fast-forward or the checkout has local edits; update by hand"
    exit 1
fi

echo "$(date -Is) update $current -> $target" >>"$LOG"
status updating "$target" "$current" "Building and starting the new version"
touch "$SYSTEM/hold"
started=$(date +%s)
git merge -q --ff-only "$target"
if deploy && rm -f "$SYSTEM/hold" && healthy "$started"; then
    status updated "$target" "$current" "Healthy after the update"
    echo "$(date -Is) healthy at $target" >>"$LOG"
    exit 0
fi

echo "$(date -Is) unhealthy at $target, rolling back to $current" >>"$LOG"
echo "$target" >>"$BAD"
reason=$(tail -n 30 "$LOG")
touch "$SYSTEM/hold"
git reset -q --hard "$current"
started=$(date +%s)
if deploy && rm -f "$SYSTEM/hold" && healthy "$started"; then
    status rolled_back "$target" "$current" "The new version was unhealthy; back on the previous one. $reason"
else
    rm -f "$SYSTEM/hold"
    status failed "$target" "$current" "Rollback did not come up healthy either; needs a human. $reason"
fi
exit 1
