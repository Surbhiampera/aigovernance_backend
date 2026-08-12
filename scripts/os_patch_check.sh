#!/usr/bin/env bash
# Checks for pending OS security updates and ALERTS — does not apply
# anything. Deliberately alert-only: automating OS-level patches on a
# client's box we don't control risks taking it down unattended with
# nobody watching (see OPERATIONS.md). Debian/Ubuntu (apt) only; any other
# OS reports "unsupported" rather than guessing at a package manager.
#
# For actual hands-off patching, see scripts/templates/ for a starter
# unattended-upgrades config (Debian/Ubuntu's own maintained tool) the
# client can review and opt into themselves — this script does not enable
# that on its own.
#
# Run from the same directory as this deployment's docker-compose.yml and
# .env — reuses the SMTP_*/TEAMS_WEBHOOK_URLS already configured there.
#
# Usage:
#   ./scripts/os_patch_check.sh
#
# Schedule it, e.g. weekly:
#   0 6 * * 1 cd /path/to/this/deployment && ./scripts/os_patch_check.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/alert.sh"

if ! command -v apt-get >/dev/null 2>&1; then
  echo "Not an apt-based system — this check doesn't support this OS. See OPERATIONS.md for manual patching guidance instead."
  exit 0
fi

TMP_OUT="$(mktemp)"
trap 'rm -f "$TMP_OUT"' EXIT

apt-get -s upgrade > "$TMP_OUT" 2>/dev/null

pending_total=$(grep -c '^Inst ' "$TMP_OUT" || true)
pending_security=$(grep '^Inst ' "$TMP_OUT" | grep -ci security || true)

echo "Pending updates: $pending_total total, $pending_security from a security pocket."

if [ "$pending_security" -gt 0 ]; then
  send_alert "AI Governance: $pending_security pending security update(s)" \
    "This deployment's host has $pending_security pending security update(s) (of $pending_total total upgradable packages). Nothing was applied automatically — review and apply manually (apt-get upgrade), or see scripts/templates/ for an unattended-upgrades config you can opt into for future updates."
fi
