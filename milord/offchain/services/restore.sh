#!/usr/bin/env bash
# Restore a backup.sh archive into the services' state volumes:  offchain/services/restore.sh <archive.tar.gz>
# Stop the services first (docker compose ... stop). Checks SHA256SUMS before writing anything, refuses to
# overwrite a non-empty volume unless RESTORE_OVERWRITE=yes, and never touches env files or keys.
# After a restore: start the services, then confirm every /healthz is 200 and the root checker reports a match.
set -euo pipefail
ARCHIVE="${1:?usage: restore.sh <milord-services-*.tar.gz>}"
PROJECT="${COMPOSE_PROJECT_NAME:-services}"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
tar -C "$TMP" -xzf "$ARCHIVE"
DIR=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
( cd "$DIR" && sha256sum -c --quiet SHA256SUMS ) || { echo "checksum mismatch: nothing restored"; exit 1; }
for V in keeper-state publisher-state checker-state monitor-state; do
  [ -f "$DIR/$V.tar" ] || continue
  NAME="${PROJECT}_${V}"
  docker volume create "$NAME" >/dev/null
  if [ "${RESTORE_OVERWRITE:-no}" != yes ] && [ -n "$(docker run --rm -v "$NAME":/data:ro alpine:3.20 ls -A /data)" ]; then
    echo "volume $NAME is not empty; set RESTORE_OVERWRITE=yes to replace it"; exit 1
  fi
  docker run --rm -v "$NAME":/data -v "$DIR":/in:ro alpine:3.20 sh -c "find /data -mindepth 1 -delete; tar -C /data -xf /in/$V.tar; chown -R 1000:1000 /data"
  echo "restored $NAME"
done
if [ -f "$DIR/deployments.json" ]; then
  cp "$DIR/deployments.json" "./restored-$(basename "$DIR").deployments.json"
  echo "deployments file from the backup: ./restored-$(basename "$DIR").deployments.json (compare with DEPLOYMENTS_HOST_FILE; not replaced automatically)"
fi
