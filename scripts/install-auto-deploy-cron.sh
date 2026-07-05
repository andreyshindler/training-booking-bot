#!/usr/bin/env bash
# One-time installer: adds a crontab entry that runs auto-deploy.sh every
# minute. Idempotent — safe to run again (replaces the existing entry).
set -eu

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_DIR/scripts/auto-deploy.sh"
chmod +x "$SCRIPT"

if ! command -v crontab >/dev/null; then
    echo "ERROR: crontab not found. Install cron first (e.g. apt install cron)." >&2
    exit 1
fi

CRON_LINE="* * * * * $SCRIPT"
( crontab -l 2>/dev/null | grep -vF "$SCRIPT" || true; echo "$CRON_LINE" ) | crontab -

echo "Installed cron entry: $CRON_LINE"
echo "Every merge to main now deploys within ~1 minute."
echo "Watch deploys with: tail -f /var/log/auto-deploy.log"
echo "(falls back to $REPO_DIR/auto-deploy.log when /var/log isn't writable)"
