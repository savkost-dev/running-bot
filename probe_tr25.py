"""uid 25: что в сыром training_readiness и почему TR=None.
Смотрим все записи + время пробуждения, проверяем фильтр 'после пробуждения'."""
import sys, json
from datetime import datetime
sys.path.insert(0, '/opt/running-bot/src')
import database as db

raw = json.loads(db.get_raw_service_data(25, 'garmin')['raw_json'])

dto = (raw.get('sleep_data') or {}).get('dailySleepDTO') or {}
wake_ms = dto.get('sleepEndTimestampLocal')
wake_local = datetime.utcfromtimestamp(int(wake_ms)/1000) if wake_ms else None
print('Пробуждение:', wake_local.isoformat() if wake_local else 'НЕТ')

tr = raw.get('training_readiness')
print('training_readiness тип:', type(tr).__name__, '| элементов:', len(tr) if isinstance(tr, list) else 1)
items = tr if isinstance(tr, list) else ([tr] if tr else [])
for it in items:
    if isinstance(it, dict):
        print('  score=%-5s tsLocal=%s level=%s' % (
            it.get('score'), it.get('timestampLocal'), it.get('level')))
    else:
        print('  не-dict:', repr(it)[:80])
