#!/bin/bash
set -euo pipefail

LOCK=/home/konstip/notetaker/.bot.lock
LOG=/home/konstip/notetaker/logs/notetaker.log

# flock prevents double-launch race condition
exec 9>"$LOCK"
flock -n 9 || exit 0  # another keepalive is running, bail

if pgrep -f "/venv/bin/python /home/konstip/notetaker/bot.py" >/dev/null; then
    exit 0  # bot is alive, nothing to do
fi

cd /home/konstip/notetaker
set -a
source .env
set +a

echo "$(date -Is) keepalive: launching bot" >> "$LOG"
nohup /home/konstip/notetaker/venv/bin/python /home/konstip/notetaker/bot.py >> "$LOG" 2>&1 &
disown
