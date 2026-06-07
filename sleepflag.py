"""Зонд: сигнал 'ночь обработана' по всем юзерам.
Читает raw_service_data, печатает по каждому сервису поля-маркеры пробуждения.
Критерий (сервис-агностичный выход awake):
  garmin: user_summary.bodyBatteryAtWakeTime не None
  coros:  dashboard ... sleepHrvData.avgSleepHrv > 0
  polar:  nightly_recharge[-1].date == сегодня (МСК)
  whoop:  recovery.records[0].created_at[:10] == сегодня (МСК)
"""
import sys, sqlite3, json
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '/opt/running-bot/src')
import database as db

MSK = timezone(timedelta(hours=3))
TODAY = datetime.now(MSK).strftime('%Y-%m-%d')

c = sqlite3.connect('running_bot.db')
c.row_factory = sqlite3.Row
users = [r['id'] for r in c.execute('SELECT id FROM users ORDER BY id')]


def svc_raw(uid, svc):
    row = db.get_raw_service_data(uid, svc)
    if not row:
        return None, None
    try:
        return json.loads(row['raw_json']), row['fetched_at']
    except Exception:
        return None, row['fetched_at']


def detect(uid):
    # какие сервисы есть
    toks = {r['service'] for r in c.execute(
        "SELECT service FROM user_tokens WHERE user_id=?", (uid,))}
    night = [s for s in ('garmin', 'coros', 'polar', 'whoop') if s in toks]
    if not night:
        return None  # нет ночного источника (только strava/ничего)

    awake = False
    detail = []

    if 'garmin' in toks:
        raw, _ = svc_raw(uid, 'garmin')
        us = (raw or {}).get('user_summary') or {}
        wake = us.get('bodyBatteryAtWakeTime')
        ok = wake is not None
        awake = awake or ok
        detail.append('garmin:wake=%s' % wake)

    if 'coros' in toks:
        raw, _ = svc_raw(uid, 'coros')
        dash = (raw or {}).get('dashboard') or {}
        info = ((dash.get('data') or {}).get('summaryInfo')) or {}
        hrv = (info.get('sleepHrvData') or {}).get('avgSleepHrv')
        ok = bool(hrv and float(hrv) > 0)
        awake = awake or ok
        detail.append('coros:sleepHrv=%s' % hrv)

    if 'polar' in toks:
        raw, _ = svc_raw(uid, 'polar')
        nr = (raw or {}).get('nightly_recharge')
        items = nr if isinstance(nr, list) else ((nr or {}).get('recharges') or (nr or {}).get('items') or [])
        items = [x for x in items if isinstance(x, dict)]
        d = items[-1].get('date') if items else None
        ok = (str(d) == TODAY)
        awake = awake or ok
        detail.append('polar:date=%s' % d)

    if 'whoop' in toks:
        raw, _ = svc_raw(uid, 'whoop')
        recs = ((raw or {}).get('recovery') or {}).get('records') or []
        d = recs[0].get('created_at', '')[:10] if recs else None
        ok = (str(d) == TODAY)
        awake = awake or ok
        detail.append('whoop:date=%s' % d)

    return awake, night, detail


print('сегодня МСК:', TODAY)
for uid in users:
    res = detect(uid)
    if res is None:
        continue
    awake, night, detail = res
    print('uid=%3s ночь_готова=%s  src=%s  [%s]' % (
        uid, 'ДА ' if awake else 'нет', ','.join(night), ' '.join(detail)))
