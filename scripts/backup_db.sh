#!/usr/bin/env bash
# Ночная копия базы бота (SQLite) с ротацией.
# Запуск: /opt/running-bot/scripts/backup_db.sh  (cron 03:30 UTC)
# Копия через sqlite3 .backup — безопасно при работающем боте (не cp).
set -euo pipefail

DB="/opt/running-bot/running_bot.db"
DIR="/opt/running-bot-backups"
KEEP=14
LOG="$DIR/backup.log"

mkdir -p "$DIR"
DATE=$(date +%F)
# Пустая дата дала бы bot_.sqlite, а ротация посчитала бы его за копию
if [[ ! "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "$(date '+%F %T') ERROR bad date '$DATE' — копия не создана" >> "$LOG"
    exit 1
fi
DST="$DIR/bot_$DATE.sqlite"

sqlite3 "$DB" ".backup '$DST'"

# Проверка, что копия целая
if [ "$(sqlite3 "$DST" 'PRAGMA integrity_check;')" != "ok" ]; then
    echo "$(date '+%F %T') ERROR integrity_check failed: $DST" >> "$LOG"
    exit 1
fi

# Ротация: оставить последние KEEP копий
ls -1t "$DIR"/bot_*.sqlite | tail -n +$((KEEP + 1)) | xargs -r rm -f

SIZE=$(du -h "$DST" | cut -f1)
COUNT=$(ls -1 "$DIR"/bot_*.sqlite | wc -l)
echo "$(date '+%F %T') OK $DST size=$SIZE copies=$COUNT" >> "$LOG"
