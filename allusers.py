import asyncio, sys, sqlite3
sys.path.insert(0, '/opt/running-bot/src')
import whoop
from data_normalizer import run_normalization

c = sqlite3.connect('running_bot.db')
c.row_factory = sqlite3.Row

users = [r['id'] for r in c.execute('SELECT id FROM users ORDER BY id')]
whoop_uids = {r['user_id'] for r in c.execute("SELECT user_id FROM user_tokens WHERE service='whoop'")}


async def main():
    print('whoop-юзеры:', sorted(whoop_uids))
    for uid in sorted(whoop_uids):
        try:
            raw = await whoop.fetch_raw(uid)
            print('  fetch uid=%s: %s' % (uid, 'ok' if raw else 'пусто'))
        except Exception as e:
            print('  fetch uid=%s: ОШИБКА %s: %s' % (uid, type(e).__name__, e))

    print('--- нормализация всех ---')
    for uid in users:
        try:
            u = run_normalization(uid)
        except Exception as e:
            print('uid=%s: ОШИБКА %s: %s' % (uid, type(e).__name__, e))
            continue
        if not u:
            continue
        w = 'Y' if 'whoop' in u.sources else '-'
        line = 'uid=%3s src=%-28s hrv=%s rhr=%s sleep_h=%s whoop=%s wmeas=%s' % (
            uid, ','.join(u.sources), u.s3_hrv, u.s3_rhr, u.s3_sleep_hours, w,
            u.data_dates.get('whoop_measured', ''))
        print(line)


asyncio.run(main())
