"""Структура training_readiness целиком (uid 2) — есть ли в нём таймстамп/запись на момент пробуждения,
или только текущее значение. Смотрим все элементы массива и поля времени."""
import sys, json
sys.path.insert(0, '/opt/running-bot/src')
import database as db

row = db.get_raw_service_data(2, 'garmin')
raw = json.loads(row['raw_json']) if row else {}
tr = raw.get('training_readiness')
print('тип:', type(tr).__name__, '| элементов:', len(tr) if isinstance(tr, list) else 1)
items = tr if isinstance(tr, list) else [tr]
for i, it in enumerate(items):
    if not isinstance(it, dict):
        continue
    print('--- элемент %d ---' % i)
    for k in sorted(it.keys()):
        if any(w in k.lower() for w in ('score','time','timestamp','date','level')):
            print('  %s = %s' % (k, it.get(k)))
