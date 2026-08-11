#!/usr/bin/env bash
# Back up THIS deployment's own database. Each client backs up their own —
# there's nothing to back up on our end, since we never hold their data
# (see LICENSING_PACKAGING.md: client self-hosts everything).
#
# Run from the same directory as this deployment's docker-compose.yml
# (the one copied from docker-compose.client.yml.example).
#
# Usage:
#   ./scripts/db_backup.sh [backup-dir]
#
# Schedule it with cron, e.g. daily at 2am:
#   0 2 * * * cd /path/to/this/deployment && ./scripts/db_backup.sh ./backups
set -euo pipefail

BACKUP_DIR="${1:-./backups}"
mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT_FILE="$BACKUP_DIR/aigovernance-${TIMESTAMP}.sql.gz"

docker compose exec -T db pg_dump -U aigovuser aigovernance | gzip > "$OUT_FILE"

echo "Wrote $OUT_FILE ($(du -h "$OUT_FILE" | cut -f1))"
