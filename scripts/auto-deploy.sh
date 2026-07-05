#!/usr/bin/env bash
# Pull-based auto-deploy. Run from cron every minute (see
# install-auto-deploy-cron.sh): fetches origin/main, and when new commits
# landed, pulls and rebuilds the bot with docker compose. Sends a ✅/❌
# Telegram message to the trainer using the bot's own token from .env.
set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="${AUTO_DEPLOY_LOG:-/var/log/auto-deploy.log}"
# fall back to a log inside the repo when /var/log isn't writable
{ touch "$LOG_FILE" 2>/dev/null; } || LOG_FILE="$REPO_DIR/auto-deploy.log"
LOCK_FILE="${TMPDIR:-/tmp}/training-booking-bot-deploy.lock"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG_FILE"; }

# never let two deploys overlap; if one is still running, skip this tick
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

cd "$REPO_DIR"

git fetch origin main --quiet 2>>"$LOG_FILE" || { log "git fetch failed"; exit 1; }
LOCAL="$(git rev-parse main)"
REMOTE="$(git rev-parse origin/main)"
[ "$LOCAL" = "$REMOTE" ] && exit 0  # nothing new — stay silent

notify() {
    local token chat_id
    token="$(grep -E '^BOT_TOKEN=' .env 2>/dev/null | head -1 | cut -d= -f2-)"
    chat_id="$(grep -E '^TRAINER_ID=' .env 2>/dev/null | head -1 | cut -d= -f2-)"
    if [ -n "$token" ] && [ -n "$chat_id" ]; then
        curl -s -o /dev/null --max-time 10 \
            "https://api.telegram.org/bot${token}/sendMessage" \
            -d chat_id="${chat_id}" --data-urlencode text="$1" || true
    fi
}

log "new commits detected: ${LOCAL:0:7} -> ${REMOTE:0:7}, deploying"
if git reset --hard origin/main >>"$LOG_FILE" 2>&1 \
    && docker compose up -d --build >>"$LOG_FILE" 2>&1; then
    SUBJECT="$(git log -1 --pretty=%s)"
    log "deploy OK: $SUBJECT"
    notify "✅ הבוט עודכן: ${SUBJECT} ($(git rev-parse --short HEAD))"
else
    log "deploy FAILED (see above)"
    notify "❌ העדכון האוטומטי של הבוט נכשל. לוג: ${LOG_FILE}"
    exit 1
fi
