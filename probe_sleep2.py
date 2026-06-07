"""Синк сырья Garmin (с новым sleep_data) для uid 2 + показ структуры сна.
Ищем точное поле конца сна = время пробуждения."""
import asyncio, sys, json
sys.path.insert(0, '/opt/running-bot/src')
import garmin
import database as db

async def main():
    await garmin.fetch_raw(2)
    row = db.get_raw_service_data(2, 'garmin')
    raw = json.loads(row['raw_json'])
    sd = raw.get('sleep_data')
    print('=== sleep_data тип:', type(sd).__name__)
    if isinstance(sd, dict):
        dto = sd.get('dailySleepDTO') or {}
        print('=== dailySleepDTO поля времени ===')
        for k in sorted(dto.keys()):
            if any(w in k.lower() for w in ('start','end','time','sleep','date','wake')):
                print('  %s = %s' % (k, dto.get(k)))
        # верхний уровень sleep_data тоже
        print('=== sleep_data верхний уровень ключи ===')
        print(' ', list(sd.keys())[:25])

asyncio.run(main())
