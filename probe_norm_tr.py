"""Проверка: что _parse_garmin_raw извлечёт из ТЕКУЩЕГО сырья uid 2 по training_readiness,
и сравнение с тем что в unified_cache. Понять — нормализатор взял бы TR сейчас или нет."""
import sys, json
sys.path.insert(0, '/opt/running-bot/src')
import database as db
from data_normalizer import _parse_garmin_raw

raw = json.loads(db.get_raw_service_data(2, 'garmin')['raw_json'])
tr = raw.get('training_readiness')
print("сырьё training_readiness тип:", type(tr).__name__, "элементов:", len(tr) if isinstance(tr,list) else 1)
if isinstance(tr, list):
    for it in tr:
        if isinstance(it, dict):
            print(f"  score={it.get('score')} @ {it.get('timestampLocal')}")

parsed = _parse_garmin_raw(raw)
print("\n_parse_garmin_raw извлёк training_readiness:", parsed.get('training_readiness'))
print("fetched_at сырья:", db.get_raw_service_data(2,'garmin')['fetched_at'])
