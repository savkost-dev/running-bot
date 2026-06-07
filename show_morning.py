"""Показать, что РЕАЛЬНО лежит в базе по утренним данным:
1) флаг morning_caught/morning_date (unified_cache)
2) что есть в unified_cache по восстановлению (НЕ снимок на утро, а последнее норм. состояние)
3) что есть в garmin_recovery_cache (TR/BB — тоже не утренний снимок)
"""
import sys, sqlite3, json
sys.path.insert(0, '/opt/running-bot/src')
import database as db

c = sqlite3.connect('running_bot.db')
c.row_factory = sqlite3.Row

rows = c.execute(
    "SELECT user_id, sources, morning_caught, morning_date, unified_json, updated_at "
    "FROM unified_cache WHERE morning_caught=1 ORDER BY user_id"
).fetchall()

print('=== unified_cache: пойманные ночи ===')
for r in rows:
    uj = {}
    try:
        uj = json.loads(r['unified_json']) if r['unified_json'] else {}
    except Exception:
        pass
    tr = uj.get('s3_training_readiness')
    tr_score = tr.get('score') if isinstance(tr, dict) else None
    print('uid=%3s caught=%s date=%s | hrv=%s rhr=%s sleep_h=%s TR=%s BB=%s | upd=%s' % (
        r['user_id'], r['morning_caught'], r['morning_date'],
        uj.get('s3_hrv'), uj.get('s3_rhr'), uj.get('s3_sleep_hours'),
        tr_score, uj.get('s3_body_battery'), (r['updated_at'] or '')[:16]))

print('\n=== garmin_recovery_cache (TR/BB, тоже НЕ утренний снимок) ===')
try:
    grc = c.execute("SELECT user_id, recovery_json, updated_at FROM garmin_recovery_cache ORDER BY user_id").fetchall()
    for r in grc:
        d = {}
        try:
            d = json.loads(r['recovery_json']) if r['recovery_json'] else {}
        except Exception:
            pass
        tr = d.get('training_readiness')
        tr_score = tr.get('score') if isinstance(tr, dict) else None
        print('uid=%3s BB=%s hrv=%s TR=%s | upd=%s' % (
            r['user_id'], d.get('body_battery'), d.get('hrv'), tr_score, (r['updated_at'] or '')[:16]))
except Exception as e:
    print('garmin_recovery_cache: колонки иные —', e)
    cols = [x[1] for x in c.execute("PRAGMA table_info(garmin_recovery_cache)")]
    print('колонки:', cols)
