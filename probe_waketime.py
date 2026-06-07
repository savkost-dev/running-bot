"""Поиск поля времени пробуждения в сыром Garmin user_summary (uid 8, 45)
и в whoop/coros/polar сырье — чтобы знать, откуда брать wake_time для снимка."""
import sys, sqlite3, json
sys.path.insert(0, '/opt/running-bot/src')
import database as db

def raw(uid, svc):
    row = db.get_raw_service_data(uid, svc)
    if not row:
        return None
    try:
        return json.loads(row['raw_json'])
    except Exception:
        return None

# --- Garmin: какие поля времени есть в user_summary ---
for uid in (8, 45):
    r = raw(uid, 'garmin') or {}
    us = r.get('user_summary') or {}
    print('=== garmin uid=%s — поля времени/сна в user_summary ===' % uid)
    for k in sorted(us.keys()):
        kl = k.lower()
        if any(w in kl for w in ('wake', 'sleep', 'time', 'sync', 'date')):
            print('  %s = %s' % (k, us.get(k)))

# --- Whoop: время конца сна ---
print('\n=== whoop uid=2 — sleep record поля времени ===')
r = raw(2, 'whoop') or {}
recs = (r.get('sleep') or {}).get('records') or []
if recs:
    s0 = recs[0]
    for k in sorted(s0.keys()):
        if any(w in k.lower() for w in ('start', 'end', 'time', 'created', 'updated')):
            print('  %s = %s' % (k, s0.get(k)))

# --- Polar: sleep_end_time ---
print('\n=== polar uid=7 — sleep поля времени ===')
r = raw(7, 'polar') or {}
sl = r.get('sleep')
nights = sl if isinstance(sl, list) else ((sl or {}).get('nights') or (sl or {}).get('items') or [])
nights = [x for x in nights if isinstance(x, dict)]
if nights:
    n = nights[-1]
    for k in sorted(n.keys()):
        if any(w in k.lower() for w in ('start', 'end', 'time', 'date')):
            print('  %s = %s' % (k, n.get(k)))

# --- COROS: что в summaryInfo про сон/время ---
print('\n=== coros uid=6 — summaryInfo поля сна/времени ===')
r = raw(6, 'coros') or {}
info = (((r.get('dashboard') or {}).get('data') or {}).get('summaryInfo')) or {}
for k in sorted(info.keys()):
    if any(w in k.lower() for w in ('sleep', 'wake', 'time', 'date')):
        print('  %s = %s' % (k, info.get(k)))
