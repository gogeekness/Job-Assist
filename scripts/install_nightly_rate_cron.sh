#!/usr/bin/env bash
# Installs (or updates) the nightly cron job that runs nightly_rate_preset.py
# against one saved search preset. Safe to re-run -- replaces any previous
# nightly_rate_preset.py cron line rather than duplicating it.
#
# Usage:
#   scripts/install_nightly_rate_cron.sh "My Preset Name" [HH:MM]
#
# The preset must already exist (save it from the Jobs page first).
# Default start time is 23:00 if not given.
set -euo pipefail

if [ -z "${1:-}" ]; then
  echo "Usage: $0 \"Preset Name\" [HH:MM]" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRESET="$1"
TIME="${2:-23:00}"
HOUR="${TIME%%:*}"
MIN="${TIME##*:}"

LOG="$REPO_DIR/backups/nightly_rate.log"
CMD="$REPO_DIR/.venv/bin/python $REPO_DIR/scripts/nightly_rate_preset.py \"$PRESET\" >> \"$LOG\" 2>&1"
CRON_LINE="$MIN $HOUR * * * $CMD"

mkdir -p "$REPO_DIR/backups"

# drop any previous nightly_rate_preset.py line, then add the new one
(crontab -l 2>/dev/null | grep -v 'nightly_rate_preset.py' ; echo "$CRON_LINE") | crontab -

echo "Installed: runs preset \"$PRESET\" nightly at $TIME, log -> $LOG"
crontab -l | grep nightly_rate_preset.py
