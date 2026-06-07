"""Разовый тест опросника: вызывает детектор+флаг напрямую, без гарда по времени окна.
Повторяет логику scheduled_wakeup_poll, но без проверки 06:00-09:00."""
import asyncio, sys
sys.path.insert(0, '/opt/running-bot/src')
from datetime import datetime, timezone, timedelta
import bot
from database import get_morning_caught, set_morning_caught, get_or_create_user, get_all_users

MSK = timezone(timedelta(hours=3))
today = datetime.now(MSK).strftime('%Y-%m-%d')


async def main():
    print('сегодня МСК:', today)
    users = get_all_users()
    for telegram_id, name, _ in users:
        db_user_id = get_or_create_user(telegram_id, name)
        svcs = bot._night_services(db_user_id)
        if not svcs:
            continue
        flag = get_morning_caught(db_user_id)
        already = bool(flag and flag.get('caught') and flag.get('date') == today)
        ready = bot._night_ready(db_user_id, today)
        if already:
            print('uid=%3s src=%-20s уже поймана (флаг %s)' % (db_user_id, ','.join(svcs), flag.get('date')))
            continue
        if ready:
            set_morning_caught(db_user_id, today)
            print('uid=%3s src=%-20s готова → флаг поставлен' % (db_user_id, ','.join(svcs)))
        else:
            print('uid=%3s src=%-20s НЕ готова (синк в реальной джобе)' % (db_user_id, ','.join(svcs)))

    print('--- проверка флагов после ---')
    for telegram_id, name, _ in users:
        db_user_id = get_or_create_user(telegram_id, name)
        if not bot._night_services(db_user_id):
            continue
        f = get_morning_caught(db_user_id)
        print('uid=%3s morning_caught=%s date=%s' % (
            db_user_id, f.get('caught') if f else None, f.get('date') if f else None))


asyncio.run(main())
