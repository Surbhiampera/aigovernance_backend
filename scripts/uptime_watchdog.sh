#!/usr/bin/env bash
# External uptime watchdog for THIS deployment. Runs OUTSIDE the container
# deliberately — if the container or the whole host is down, nothing
# inside the container can alert about it. Checks /health, alerts once
# after N consecutive failures (avoids alerting on a single network blip),
# and once more on recovery. Stays quiet during an ongoing outage instead
# of re-alerting every run.
#
# Run from the same directory as this deployment's docker-compose.yml and
# .env — reuses the SMTP_*/TEAMS_WEBHOOK_URLS already configured there
# (see OPERATIONS.md).
#
# Usage:
#   ./scripts/uptime_watchdog.sh [health-url] [failure-threshold]
#
# Schedule it, e.g. every 5 minutes via cron:
#   */5 * * * * cd /path/to/this/deployment && ./scripts/uptime_watchdog.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/alert.sh"

HEALTH_URL="${1:-http://localhost:8000/health}"
FAILURE_THRESHOLD="${2:-3}"
STATE_FILE="./.watchdog_state"

if curl -sf -o /dev/null --max-time 10 "$HEALTH_URL"; then
  if [ -f "$STATE_FILE" ]; then
    prev_count=$(cat "$STATE_FILE")
    if [ "$prev_count" -ge "$FAILURE_THRESHOLD" ]; then
      send_alert "AI Governance: deployment back up" \
        "$HEALTH_URL is responding again after $prev_count consecutive failed check(s)."
    fi
    rm -f "$STATE_FILE"
  fi
  echo "OK: $HEALTH_URL is healthy"
  exit 0
fi

count=1
[ -f "$STATE_FILE" ] && count=$(($(cat "$STATE_FILE") + 1))
echo "$count" > "$STATE_FILE"

if [ "$count" -eq "$FAILURE_THRESHOLD" ]; then
  send_alert "AI Governance: deployment appears DOWN" \
    "$HEALTH_URL has failed $count consecutive check(s). Investigate this deployment's server/container."
fi

echo "DOWN: $HEALTH_URL failed (consecutive failures: $count)"
