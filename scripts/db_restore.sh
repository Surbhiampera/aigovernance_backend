#!/usr/bin/env bash
# Restore THIS deployment's database from a backup made by db_backup.sh.
# DESTRUCTIVE — replaces all current data in the database. Run from the
# same directory as this deployment's docker-compose.yml.
#
# Usage:
#   ./scripts/db_restore.sh path/to/aigovernance-<timestamp>.sql.gz
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <backup-file.sql.gz>" >&2
  exit 1
fi

BACKUP_FILE="$1"
if [ ! -f "$BACKUP_FILE" ]; then
  echo "No such file: $BACKUP_FILE" >&2
  exit 1
fi

echo "This will REPLACE all data in the 'aigovernance' database with the contents of $BACKUP_FILE."
read -r -p "Type 'restore' to continue: " CONFIRM
if [ "$CONFIRM" != "restore" ]; then
  echo "Aborted."
  exit 1
fi

gunzip -c "$BACKUP_FILE" | docker compose exec -T db psql -U aigovuser -d aigovernance

echo "Restored from $BACKUP_FILE"
