"""Показать флаг пойманной ночи + замороженный снимок на утро по всем юзерам с ночным сервисом."""
import sys, sqlite3
sys.path.insert(0, '/opt/running-bot/src')

c = sqlite3.connect('running_bot.db')
c.row_factory = sqlite3.Row

rows = c.execute(
    "SELECT user_id, morning_caught, morning_date, morning_tr, morning_bb, "
    "morning_hrv, morning_rhr, morning_sleep_h, morning_wake_at, morning_snapshot_at "
    "FROM unified_cache WHERE morning_caught=1 ORDER BY user_id"
).fetchall()

print('=== Флаг ночи + снимок на утро ===')
for r in rows:
    print('uid=%3s [%s] TR=%s BB=%s HRV=%s RHR=%s сон=%sч пробужд=%s' % (
        r['user_id'], r['morning_date'],
        r['morning_tr'], r['morning_bb'], r['morning_hrv'], r['morning_rhr'],
        r['morning_sleep_h'],
        (r['morning_wake_at'] or '—')[:16]))

print('\nВсего пойманных ночей: %d' % len(rows))
print('Со снимком (есть хоть одно поле): %d' % sum(
    1 for r in rows if any(r[k] is not None for k in
        ('morning_tr','morning_bb','morning_hrv','morning_rhr','morning_sleep_h','morning_wake_at'))))
