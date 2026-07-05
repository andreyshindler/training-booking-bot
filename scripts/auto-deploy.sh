#!/usr/bin/env bash
# Pull-based auto-deploy. Run from cron every minute (see
# install-auto-deploy-cron.sh): fetches origin/main, and when new commits
# landed, pulls and rebuilds the bot with docker compose. Sends the trainer a
# short ✅ Telegram message on success; on failure, sends the full log of the
# failed run as a .txt document. Uses the bot's own token from .env.
set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="${AUTO_DEPLOY_LOG:-$REPO_DIR/auto-deploy.log}"
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

_bot_token() { grep -E '^BOT_TOKEN=' .env 2>/dev/null | head -1 | cut -d= -f2-; }
_chat_id() { grep -E '^TRAINER_ID=' .env 2>/dev/null | head -1 | cut -d= -f2-; }

notify() {
    local token chat_id
    token="$(_bot_token)"; chat_id="$(_chat_id)"
    if [ -n "$token" ] && [ -n "$chat_id" ]; then
        curl -s -o /dev/null --max-time 10 \
            "https://api.telegram.org/bot${token}/sendMessage" \
            -d chat_id="${chat_id}" --data-urlencode text="$1" || true
    fi
}

# $1 = file to attach, $2 = caption. Falls back to a plain message if the
# document upload itself fails.
notify_document() {
    local token chat_id
    token="$(_bot_token)"; chat_id="$(_chat_id)"
    if [ -n "$token" ] && [ -n "$chat_id" ]; then
        curl -s -o /dev/null --max-time 30 \
            "https://api.telegram.org/bot${token}/sendDocument" \
            -F chat_id="${chat_id}" \
            -F caption="$2" \
            -F document=@"$1" \
            || notify "$2 (לא הצלחתי לצרף את קובץ הלוג — ראו ${LOG_FILE})"
    fi
}

# capture this run's output separately so a failure can be sent as a file
RUN_LOG="$(mktemp "${TMPDIR:-/tmp}/deploy-failed-$(date '+%Y%m%d-%H%M%S')-XXXX.txt")"
trap 'rm -f "$RUN_LOG"' EXIT

log "new commits detected: ${LOCAL:0:7} -> ${REMOTE:0:7}, deploying"
{
    echo "=== auto-deploy $(date '+%Y-%m-%d %H:%M:%S') ==="
    echo "=== ${LOCAL:0:7} -> ${REMOTE:0:7} ==="
    git reset --hard origin/main 2>&1 \
        && docker compose up -d --build 2>&1
} >>"$RUN_LOG"
STATUS=$?
cat "$RUN_LOG" >>"$LOG_FILE"

if [ "$STATUS" -eq 0 ]; then
    SUBJECT="$(git log -1 --pretty=%s)"
    log "deploy OK: $SUBJECT"
    notify "✅ הבוט עודכן: ${SUBJECT} ($(git rev-parse --short HEAD))"
else
    log "deploy FAILED (see above)"
    notify_document "$RUN_LOG" "❌ העדכון האוטומטי של הבוט נכשל — הלוג המלא מצורף."
    exit 1
fi
