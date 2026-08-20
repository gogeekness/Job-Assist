#!/usr/bin/env bash
# Backs up the gitignored, personal, non-versioned Job-Assist data --
# jobs.db, generated CVs, the curated bullet-bank CSV, and local config --
# on a cron schedule (see scripts/install_backup_cron.sh). The large,
# rarely-changing archived-CV folders (~200MB) are intentionally excluded
# from this recurring backup; back those up separately/manually.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$REPO_DIR/backups"
KEEP=28  # 28 x 6h = 7 days of history

mkdir -p "$BACKUP_DIR"
cd "$REPO_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/job-assist-backup-$STAMP.tar.gz"

tar -czf "$OUT" \
  jobs.db \
  generated/ \
  richard_cv_master_bullet_bank_g_update.csv \
  .env \
  profile.local.json \
  config.local.json \
  active-settings/ \
  2>/dev/null || true

# Prune down to the newest $KEEP backups
ls -1t "$BACKUP_DIR"/job-assist-backup-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "$(date -Iseconds) backed up -> $OUT"
