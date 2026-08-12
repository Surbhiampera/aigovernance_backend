#!/usr/bin/env bash
# Shared alert helper for host-level scripts (uptime_watchdog.sh,
# os_patch_check.sh) that run OUTSIDE the app container — so they can't
# import app.services.notification_service directly. Reads the SAME env
# var names from .env in the current directory (SMTP_*, NOTIFICATION_EMAIL,
# TEAMS_WEBHOOK_URLS — see docker-compose.client.yml.example) and sends via
# curl, which these scripts already depend on for the health/update checks
# themselves — no extra dependencies.
#
# Usage: source this file, then call:
#   send_alert "Subject line" "Body text"

_load_env() {
  if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
  fi
}

_send_email_alert() {
  local subject="$1" body="$2"
  [ -z "${SMTP_HOST:-}" ] && return 0
  [ -z "${NOTIFICATION_EMAIL:-}" ] && return 0

  local from="${SMTP_FROM_EMAIL:-${SMTP_USER:-alerts@aigovernance.local}}"
  local rcpt_args=()
  IFS=',' read -ra recipients <<< "$NOTIFICATION_EMAIL"
  for r in "${recipients[@]}"; do
    rcpt_args+=(--mail-rcpt "$(echo "$r" | xargs)")
  done

  local auth_args=()
  [ -n "${SMTP_USER:-}" ] && auth_args=(-u "${SMTP_USER}:${SMTP_PASSWORD:-}")

  {
    echo "From: $from"
    echo "To: $NOTIFICATION_EMAIL"
    echo "Subject: $subject"
    echo
    echo "$body"
  } | curl -s --max-time 15 --url "smtp://${SMTP_HOST}:${SMTP_PORT:-587}" \
      --mail-from "$from" \
      "${rcpt_args[@]}" \
      "${auth_args[@]}" \
      --upload-file - 2>/dev/null
  return 0
}

_send_teams_alert() {
  local subject="$1" body="$2"
  [ -z "${TEAMS_WEBHOOK_URLS:-}" ] && return 0

  local escaped_body
  escaped_body=$(printf '%s' "$body" | sed ':a;N;$!ba;s/\n/<br>/g' | sed 's/"/\\"/g')
  local payload
  payload=$(printf '{"@type":"MessageCard","@context":"http://schema.org/extensions","summary":"%s","sections":[{"activityTitle":"%s","activityText":"%s"}]}' \
    "$subject" "$subject" "$escaped_body")

  IFS=',' read -ra webhooks <<< "$TEAMS_WEBHOOK_URLS"
  for hook in "${webhooks[@]}"; do
    curl -s --max-time 15 -X POST -H "Content-Type: application/json" -d "$payload" "$(echo "$hook" | xargs)" >/dev/null 2>&1
  done
  return 0
}

send_alert() {
  local subject="$1" body="$2"
  _load_env
  _send_email_alert "$subject" "$body"
  _send_teams_alert "$subject" "$body"
  return 0
}
