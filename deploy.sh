#!/usr/bin/env bash
#
# Fix bind-mount ownership, then (re)deploy the monitor with docker compose.
#
# The container runs as a fixed uid:gid (docker-compose.yml -> user:). The host
# ./data and ./logs dirs MUST be owned by that same uid:gid or the container
# gets "Permission denied" writing state.json / monitor.log. This script keeps
# the two in sync and brings the stack up.
#
# Usage:
#   ./deploy.sh              # uses PUID/PGID below (default 1000:1000)
#   PUID=568 PGID=568 ./deploy.sh   # override to match your host user
#
set -euo pipefail

# uid:gid the container writes as. MUST match `user:` in docker-compose.yml.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Run from the directory this script lives in, so relative paths are stable.
cd "$(dirname "$0")"

echo "==> Ensuring ./data and ./logs exist"
mkdir -p data logs

echo "==> Chowning ./data and ./logs to ${PUID}:${PGID}"
if ! chown -R "${PUID}:${PGID}" data logs 2>/dev/null; then
  echo "    (need root for chown) retrying with sudo..."
  sudo chown -R "${PUID}:${PGID}" data logs
fi

echo "==> Removing any stale 'pv-dossard' container (e.g. from a manual docker run)"
# Compose only recreates containers it created; one left by `docker run` would
# otherwise cause a name collision. Ignore the error if none exists.
docker rm -f pv-dossard >/dev/null 2>&1 || true

echo "==> Building and starting the stack (docker compose up -d --build)"
docker compose up -d --build

echo "==> Done. Recent logs:"
docker compose logs --tail 10
echo
echo "Follow live with:  docker compose logs -f"
