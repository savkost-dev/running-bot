"""Прицельный разбор: почему ночь не закрылась у garmin 9/13 и polar 7."""
import sys, sqlite3, json
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '/opt/running-bot/src')
import database as db

MSK = timezone(timedelta(hours=3))
TODAY = datetime.now(MSK).strftime('%Y-%m-%d')
print('сегодня МСК:', TODAY)


def raw(uid, svc):
    row = db.get_raw_service_data(uid, svc)
    if not row:
        return None, None
    try:
        return json.loads(row['raw_json']), row['fetched_at']
    except Exception:
        return None, row['fetched_at']


# --- Garmin 9, 13: что в user_summary про сон/восстановление ---
for uid in (9, 13):
    r, fetched = raw(uid, 'garmin')
    us = (r or {}).get('user_summary') or {}
    hrv = (r or {}).get('hrv_data') or {}
    tr = (r or {}).get('training_readiness')
    print('\n=== garmin uid=%s fetched=%s ===' % (uid, fetched))
    for k in ('bodyBatteryAtWakeTime', 'bodyBatteryDuringSleep',
              'bodyBatteryMostRecentValue', 'bodyBatteryHighestValue',
              'sleepingSeconds', 'measurableAwakeDuration', 'measurableAsleepDuration',
              'lastSyncTimestampGMT', 'restingHeartRate'):
        print('  us.%s = %s' % (k, us.get(k)))
    hsum = (hrv.get('hrvSummary') or {}) if isinstance(hrv, dict) else {}
    print('  hrv.lastNightAvg =', hsum.get('lastNightAvg'))
    print('  hrv.weeklyAvg =', hsum.get('weeklyAvg'))
    print('  training_readiness =', 'есть' if tr else tr, '| len=', len(tr) if isinstance(tr, list) else '-')


# --- Polar 7: вся структура nightly_recharge ---
r, fetched = raw(7, 'polar')
print('\n=== polar uid=7 fetched=%s ===' % fetched)
nr = (r or {}).get('nightly_recharge')
print('  nightly_recharge type:', type(nr).__name__)
items = nr if isinstance(nr, list) else ((nr or {}).get('recharges') or (nr or {}).get('items') or [])
items = [x for x in items if isinstance(x, dict)]
print('  записей:', len(items))
for it in items[-3:]:
    print('   date=%s ans=%s hrv=%s' % (
        it.get('date'), it.get('ans_charge'), it.get('heart_rate_variability_avg')))
sl = (r or {}).get('sleep')
slitems = sl if isinstance(sl, list) else ((sl or {}).get('nights') or (sl or {}).get('items') or [])
slitems = [x for x in slitems if isinstance(x, dict)]
print('  sleep записей:', len(slitems))
for it in slitems[-3:]:
    print('   sleep date=%s end=%s' % (it.get('date'), it.get('sleep_end_time')))
