#!/usr/bin/env bash
# Snapshot of the MILORD services' recoverable state:  offchain/services/backup.sh [out-dir]   (from milord/)
#
#   - the four state volumes (keeper, publisher incl. epochs/<k>.json, checker, monitor) as tar files
#   - the deployments file the services mount (DEPLOYMENTS_HOST_FILE)
#   - SHA256SUMS over everything
# Never included: *.env files and keys (they live in the secret store). Everything here is also recoverable from
# chain (the publisher rebuilds every epoch from genesis), so this backup is for fast restarts and for the
# published allocation files; the engine's ledgers (bot/arb_monitor) have their own backup (OPERATIONS_PLAN.md §6).
# Optional: BACKUP_AGE_RECIPIENT=age1... encrypts the archive with `age` before it leaves the host.
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT="${1:-./backups}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DIR="$OUT/milord-services-$STAMP"
PROJECT="${COMPOSE_PROJECT_NAME:-services}"
DEP="${DEPLOYMENTS_HOST_FILE:-deployments/robinhood-46630.json}"
mkdir -p "$DIR"
for V in keeper-state publisher-state checker-state monitor-state; do
  NAME="${PROJECT}_${V}"
  if docker volume inspect "$NAME" >/dev/null 2>&1; then
    docker run --rm -v "$NAME":/data:ro -v "$(cd "$DIR" && pwd)":/out alpine:3.20 tar -C /data -cf "/out/$V.tar" .
  else
    echo "volume $NAME not found (COMPOSE_PROJECT_NAME?); skipped" >&2
  fi
done
[ -f "$DEP" ] && cp "$DEP" "$DIR/deployments.json" || echo "deployments file $DEP not found; skipped" >&2
( cd "$DIR" && sha256sum -- * > SHA256SUMS )
tar -C "$OUT" -czf "$DIR.tar.gz" "$(basename "$DIR")" && rm -rf "$DIR"
if [ -n "${BACKUP_AGE_RECIPIENT:-}" ]; then
  age -r "$BACKUP_AGE_RECIPIENT" -o "$DIR.tar.gz.age" "$DIR.tar.gz" && rm -f "$DIR.tar.gz"
  echo "$DIR.tar.gz.age"
else
  echo "$DIR.tar.gz"
fi
