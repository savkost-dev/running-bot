"""Поиск ВЕРНОГО поля времени пробуждения в Garmin.
wellnessEndTimeLocal ползёт (= последний замер днём). Нужен конец фазы СНА.
Смотрим sleep_data / sleepEndTimestamp у uid 4, 47 (ползут) и 8 (норм)."""
import sys, json
sys.path.insert(0, '/opt/running-bot/src')
import database as db

def raw(uid, svc):
    row = db.get_raw_service_data(uid, svc)
    if not row: return None
    try: return json.loads(row['raw_json'])
    except: return None

for uid in (4, 47, 8):
    r = raw(uid, 'garmin') or {}
    print('=== uid=%s ключи garmin сырья верхнего уровня ===' % uid)
    print(' ', list(r.keys()))
    # user_summary — wellness время
    us = r.get('user_summary') or {}
    print('  user_summary: wellnessEndTimeLocal=%s sleepingSeconds=%s' % (
        us.get('wellnessEndTimeLocal'), us.get('sleepingSeconds')))
    # ищем sleep-блок
    for key in r.keys():
        if 'sleep' in key.lower():
            sd = r[key]
            print('  [%s] тип=%s' % (key, type(sd).__name__))
            if isinstance(sd, dict):
                # типичные поля Garmin sleep
                dto = sd.get('dailySleepDTO') or sd
                for f in ('sleepStartTimestampLocal','sleepEndTimestampLocal',
                          'sleepStartTimestampGMT','sleepEndTimestampGMT',
                          'calendarDate'):
                    if isinstance(dto, dict) and f in dto:
                        print('      %s = %s' % (f, dto.get(f)))
                # на всякий показать все ключи sleep-блока
                if isinstance(dto, dict):
                    print('      (ключи sleep:', list(dto.keys())[:20], ')')
    print()
