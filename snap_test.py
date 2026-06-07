"""Проверка: 1) ключи get_garmin_recovery_cache; 2) сбор снимка _collect_morning_snapshot
для разных сервисов. Без записи в БД — только показать, что соберётся."""
import sys
sys.path.insert(0, '/opt/running-bot/src')
import bot
from database import get_garmin_recovery_cache

print('=== get_garmin_recovery_cache(8) ключи ===')
grc = get_garmin_recovery_cache(8)
print(type(grc), grc if not grc else list(grc.keys()))
if grc:
    print('  tr_score=%s hrv_last_night=%s body_battery=%s' % (
        grc.get('tr_score'), grc.get('hrv_last_night'), grc.get('body_battery')))

print('\n=== _collect_morning_snapshot по юзерам ===')
# 2=Garmin+Whoop, 8/45=Garmin, 6=COROS, 7=Polar
for uid in (2, 8, 45, 6, 7):
    snap = bot._collect_morning_snapshot(uid)
    print('uid=%3s tr=%s bb=%s hrv=%s rhr=%s sleep_h=%s wake_at=%s' % (
        uid, snap['tr'], snap['bb'], snap['hrv'], snap['rhr'],
        snap['sleep_h'], snap['wake_at']))
