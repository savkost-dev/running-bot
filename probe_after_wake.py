"""Проверка: можно ли отфильтровать TR 'после пробуждения' и в какой зоне сравнивать.
wake = sleepEndTimestampLocal (мс лок). TR timestamp (GMT) и timestampLocal.
Сверим на uid 2."""
import sys, json
from datetime import datetime
sys.path.insert(0, '/opt/running-bot/src')
import database as db

raw = json.loads(db.get_raw_service_data(2, 'garmin')['raw_json'])
dto = (raw.get('sleep_data') or {}).get('dailySleepDTO') or {}
wake_ms = dto.get('sleepEndTimestampLocal')
wake_local = datetime.utcfromtimestamp(int(wake_ms)/1000)
print('Пробуждение (sleepEndTimestampLocal):', wake_local.isoformat())

print('\nTR записи:')
tr = raw.get('training_readiness') or []
for it in (tr if isinstance(tr, list) else [tr]):
    if isinstance(it, dict) and it.get('score') is not None:
        ts_gmt = it.get('timestamp')          # GMT
        ts_loc = it.get('timestampLocal')     # local
        # сравним timestampLocal с wake_local
        try:
            after = datetime.fromisoformat(ts_loc.rstrip('0').rstrip('.') if '.' in ts_loc else ts_loc) >= wake_local
        except Exception:
            after = '?'
        print('  score=%-4s tsLocal=%s  после пробужд(local)=%s' % (
            it.get('score'), ts_loc, after))

# что выберется: min timestampLocal среди тех что >= wake_local
print('\nВыбор: первая запись с timestampLocal >= пробуждения:')
cands = []
for it in (tr if isinstance(tr, list) else [tr]):
    if isinstance(it, dict) and it.get('score') is not None and it.get('timestampLocal'):
        cands.append(it)
def _loc(t):
    s = t.get('timestampLocal')
    try: return datetime.fromisoformat(s)
    except: return datetime.max
after_wake = [t for t in cands if _loc(t) >= wake_local]
pick = min(after_wake, key=_loc) if after_wake else (min(cands, key=_loc) if cands else None)
print('  ВЫБРАНО score=%s @ %s' % (pick.get('score'), pick.get('timestampLocal')) if pick else '  нет')
