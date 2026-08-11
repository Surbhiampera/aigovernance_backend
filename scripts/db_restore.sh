#!/usr/bin/env bash
# Restore THIS deployment's database from a backup made by db_backup.sh.
# DESTRUCTIVE — drops and recreates the 'aigovernance' database entirely,
# replacing ALL current data with the backup's contents. Run from the same
# directory as this deployment's docker-compose.yml.
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

echo "This will DROP and RECREATE the 'aigovernance' database, replacing ALL current data with the contents of $BACKUP_FILE."
read -r -p "Type 'restore' to continue: " CONFIRM
if [ "$CONFIRM" != "restore" ]; then
  echo "Aborted."
  exit 1
fi

# Drop and recreate cleanly so the replay below never collides with whatever
# state the database happened to be in — a partial DROP TABLE beforehand, a
# half-migrated schema, anything. Connects to the 'postgres' maintenance
# database since a database can't drop itself while connected to it.
docker compose exec -T db psql -U aigovuser -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'aigovernance' AND pid <> pg_backend_pid();"
docker compose exec -T db psql -U aigovuser -d postgres -c "DROP DATABASE IF EXISTS aigovernance;"
docker compose exec -T db psql -U aigovuser -d postgres -c "CREATE DATABASE aigovernance OWNER aigovuser;"

gunzip -c "$BACKUP_FILE" | docker compose exec -T db psql -U aigovuser -d aigovernance

echo "Restored from $BACKUP_FILE"
