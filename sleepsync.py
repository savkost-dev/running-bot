"""Итерация синка по флагу законченной ночи.
1) флаг ДО: кто из юзеров с ночным сервисом ещё не закрыл сегодняшнюю ночь
2) синк сырья (fetch_raw) только для незакрытых
3) флаг ПОСЛЕ: пересмотр
Критерий 'ночь готова' (сервис-агностичный выход):
  garmin: user_summary.bodyBatteryAtWakeTime не None
  coros:  sleepHrvData.avgSleepHrv > 0
  polar:  nightly_recharge[-1].date == сегодня (МСК)
  whoop:  recovery.records[0].created_at[:10] == сегодня (МСК)
"""
import asyncio, sys, sqlite3, json
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '/opt/running-bot/src')
import database as db

MSK = timezone(timedelta(hours=3))
TODAY = datetime.now(MSK).strftime('%Y-%m-%d')

c = sqlite3.connect('running_bot.db')
c.row_factory = sqlite3.Row
users = [r['id'] for r in c.execute('SELECT id FROM users ORDER BY id')]


def toks(uid):
    return {r['service'] for r in c.execute(
        "SELECT service FROM user_tokens WHERE user_id=?", (uid,))}


def svc_raw(uid, svc):
    row = db.get_raw_service_data(uid, svc)
    if not row:
        return None
    try:
        return json.loads(row['raw_json'])
    except Exception:
        return None


def night_ready(uid):
    """Возвращает (ready: bool|None, night_services: list, detail: list).
    None — нет ночного сервиса."""
    t = toks(uid)
    night = [s for s in ('garmin', 'coros', 'polar', 'whoop') if s in t]
    if not night:
        return None, [], []
    ready = False
    detail = []
    if 'garmin' in t:
        us = (svc_raw(uid, 'garmin') or {}).get('user_summary') or {}
        wake = us.get('bodyBatteryAtWakeTime')
        ready = ready or (wake is not None)
        detail.append('garmin:wake=%s' % wake)
    if 'coros' in t:
        dash = (svc_raw(uid, 'coros') or {}).get('dashboard') or {}
        info = ((dash.get('data') or {}).get('summaryInfo')) or {}
        hrv = (info.get('sleepHrvData') or {}).get('avgSleepHrv')
        ready = ready or bool(hrv and float(hrv) > 0)
        detail.append('coros:sleepHrv=%s' % hrv)
    if 'polar' in t:
        nr = (svc_raw(uid, 'polar') or {}).get('nightly_recharge')
        items = nr if isinstance(nr, list) else ((nr or {}).get('recharges') or (nr or {}).get('items') or [])
        items = [x for x in items if isinstance(x, dict)]
        d = items[-1].get('date') if items else None
        ready = ready or (str(d) == TODAY)
        detail.append('polar:date=%s' % d)
    if 'whoop' in t:
        recs = ((svc_raw(uid, 'whoop') or {}).get('recovery') or {}).get('records') or []
        d = recs[0].get('created_at', '')[:10] if recs else None
        ready = ready or (str(d) == TODAY)
        detail.append('whoop:date=%s' % d)
    return ready, night, detail


async def sync_user(uid):
    """Дёргает свежее сырьё по всем ночным сервисам юзера."""
    t = toks(uid)
    if 'garmin' in t:
        import garmin as _g
        await _g.fetch_raw(uid)
    if 'coros' in t:
        import coros as _c
        await _c.fetch_raw(uid)
    if 'polar' in t:
        import polar as _p
        await _p.fetch_raw(uid)
    if 'whoop' in t:
        import whoop as _w
        await _w.fetch_raw(uid)


def print_flags(label):
    print('=== флаг %s (сегодня МСК %s) ===' % (label, TODAY))
    not_ready = []
    for uid in users:
        ready, night, detail = night_ready(uid)
        if ready is None:
            continue
        print('uid=%3s готова=%s src=%s [%s]' % (
            uid, 'ДА ' if ready else 'нет', ','.join(night), ' '.join(detail)))
        if not ready:
            not_ready.append(uid)
    return not_ready


async def main():
    not_ready = print_flags('ДО синка')
    print('\nСинкаю незакрытых:', not_ready)
    for uid in not_ready:
        try:
            await sync_user(uid)
            print('  synced uid=%s' % uid)
        except Exception as e:
            print('  uid=%s ОШИБКА %s: %s' % (uid, type(e).__name__, e))
        await asyncio.sleep(1)
    print()
    print_flags('ПОСЛЕ синка')


asyncio.run(main())
