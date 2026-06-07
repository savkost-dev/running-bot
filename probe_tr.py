"""Структура training_readiness в сыром Garmin (uid 2) — взять score для снимка напрямую из сырья."""
import sys, json
sys.path.insert(0, '/opt/running-bot/src')
import database as db

for uid in (2, 8):
    row = db.get_raw_service_data(uid, 'garmin')
    raw = json.loads(row['raw_json']) if row else {}
    tr = raw.get('training_readiness')
    print('=== uid=%s training_readiness тип=%s ===' % (uid, type(tr).__name__))
    item = tr[0] if isinstance(tr, list) and tr else tr
    if isinstance(item, dict):
        print('  score=%s level=%s' % (item.get('score'), item.get('level')))
