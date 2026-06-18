#!/bin/sh
set -eu

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

export PGPASSWORD="$POSTGRES_PASSWORD"

BACKUP_INTERVAL_SECONDS="${BRAIN_BACKUP_INTERVAL_SECONDS:-86400}"
BACKUP_RETENTION_DAYS="${BRAIN_BACKUP_RETENTION_DAYS:-7}"

mkdir -p /backups

run_backup() {
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    tmp="/backups/brain-${timestamp}.dump.tmp"
    out="/backups/brain-${timestamp}.dump"

    pg_dump -h postgres -U brain -d brain -Fc -f "$tmp"
    mv "$tmp" "$out"
    find /backups -type f -name 'brain-*.dump' -mtime "+${BACKUP_RETENTION_DAYS}" -delete
    echo "backup written: $out"
}

while true; do
    run_backup
    if [ "${BRAIN_BACKUP_ONCE:-false}" = "true" ]; then
        exit 0
    fi
    sleep "$BACKUP_INTERVAL_SECONDS"
done
