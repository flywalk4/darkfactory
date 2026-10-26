#!/bin/sh
# Start a worker on the new image right away and let the old one drain in the background.
# Task slots are counted in the database and runs are leased, so two workers can overlap safely;
# a draining worker stops its scheduler loop at once and only finishes the steps it already runs.
set -eu
cd "$(dirname "$0")/.."

# A worker stopped without its cleanup (host restart mid-drain) would be started again on its old image
# by --no-recreate below.
docker compose ps -aq --status exited worker | xargs -r docker rm >/dev/null
old=$(docker compose ps -q worker)
count=$(printf '%s\n' "$old" | grep -c . || true)
docker compose build -q worker
docker compose up -d --no-deps --no-recreate --scale "worker=$((count + 1))" worker
for id in $old; do
    # Stop with the compose grace period, then drop the container so the next deploy starts from one.
    nohup sh -c "docker stop -t 960 $id >/dev/null && docker rm $id >/dev/null" >/dev/null 2>&1 &
done
echo "new worker started; draining: ${old:-none}"
