"""Сброс флагов morning_caught + повторная запись флага СО снимком (без гарда по окну).
Имитирует scheduled_wakeup_poll по текущему сырью."""
import asyncio, sys, sqlite3
sys.path.insert(0, '/opt/running-bot/src')
from datetime import datetime, timezone, timedelta
import bot
from database import get_or_create_user, get_all_users, set_morning_caught

MSK = timezone(timedelta(hours=3))
today = datetime.now(MSK).strftime('%Y-%m-%d')

# 1) сброс флагов
c = sqlite3.connect('running_bot.db')
c.execute("UPDATE unified_cache SET morning_caught=0, morning_date=NULL, "
          "morning_tr=NULL, morning_bb=NULL, morning_hrv=NULL, morning_rhr=NULL, "
          "morning_sleep_h=NULL, morning_wake_at=NULL, morning_snapshot_at=NULL")
c.commit()
c.close()
print('Флаги сброшены. Прогон записи снимка по текущему сырью...\n')

# 2) запись флага со снимком по готовым ночам
async def main():
    users = get_all_users()
    for telegram_id, name, _ in users:
        db_user_id = get_or_create_user(telegram_id, name)
        if not bot._night_services(db_user_id):
            continue
        if bot._night_ready(db_user_id, today):
            snap = bot._collect_morning_snapshot(db_user_id)
            set_morning_caught(db_user_id, today, snapshot=snap)
            print('uid=%3s записан снимок: TR=%s BB=%s HRV=%s RHR=%s сон=%s wake=%s' % (
                db_user_id, snap['tr'], snap['bb'], snap['hrv'], snap['rhr'],
                snap['sleep_h'], (snap['wake_at'] or '—')[:16]))

asyncio.run(main())
