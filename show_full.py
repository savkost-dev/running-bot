"""Полная картина по всем юзерам с ночным сервисом: флаг ночи + слепок + TR из garmin_recovery_cache.
Показывает и тех, у кого ночь НЕ поймана (caught=0) — для них слепок пуст, но TR из кэша виден."""
import sys, sqlite3, json
sys.path.insert(0, '/opt/running-bot/src')
import database as db

c = sqlite3.connect('running_bot.db')
c.row_factory = sqlite3.Row

# все юзеры с ночным сервисом (garmin/coros/polar/whoop)
night = []
for r in c.execute("SELECT id, COALESCE(username, name, 'uid_'||id) AS nm FROM users ORDER BY id"):
    uid = r['id']
    svcs = [s for s in ('garmin','coros','polar','whoop') if db.get_token(uid, s)]
    if svcs:
        night.append((uid, r['nm'], ','.join(svcs)))

# снимок/флаг
uc = {r['user_id']: r for r in c.execute(
    "SELECT user_id, morning_caught, morning_date, morning_tr, morning_bb, morning_hrv, "
    "morning_rhr, morning_sleep_h, morning_wake_at FROM unified_cache")}

# TR/BB из garmin_recovery_cache
grc = {}
for r in c.execute("SELECT user_id, tr_score, body_battery, hrv_last_night FROM garmin_recovery_cache"):
    grc[r['user_id']] = r

print('=== Полная картина по ночным юзерам (%d) ===' % len(night))
print('caught | uid | имя | TR(снимок/кэш) | BB | HRV | RHR | сон | пробужд')
for uid, nm, svcs in night:
    s = uc.get(uid)
    caught = bool(s and s['morning_caught'])
    tr_snap = s['morning_tr'] if s else None
    tr_cache = grc[uid]['tr_score'] if uid in grc else None
    bb = s['morning_bb'] if s else None
    hrv = s['morning_hrv'] if s else None
    rhr = s['morning_rhr'] if s else None
    sleep = s['morning_sleep_h'] if s else None
    wake = (s['morning_wake_at'] or '—') if s else '—'
    wake_hm = wake[11:16] if len(wake) >= 16 else wake
    flag = '✅' if caught else '❌'
    print('%s uid=%3s %-14s | TR=%s/%s | BB=%-4s HRV=%-5s RHR=%-3s сон=%-5s пробужд=%-5s [%s]' % (
        flag, uid, str(nm)[:14], tr_snap, tr_cache, bb, hrv, rhr, sleep, wake_hm, svcs))
