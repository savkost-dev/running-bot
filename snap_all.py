import sys
sys.path.insert(0, '/opt/running-bot/src')
from database import get_all_users, get_or_create_user, get_morning_caught

print("=== morning_caught по всем пользователям ===")
for telegram_id, name, _ in get_all_users():
    uid = get_or_create_user(telegram_id, name)
    s = get_morning_caught(uid)
    if not s:
        print(f"uid {uid:>3} {name:<16} — None (нет записи)")
        continue
    print(f"uid {uid:>3} {name:<16} caught={s.get('caught')} date={s.get('date')} "
          f"TR={s.get('tr')} BB={s.get('bb')} HRV={s.get('hrv')} RHR={s.get('rhr')} "
          f"sleep={s.get('sleep_h')} wake={s.get('wake_at')} snap_at={s.get('snapshot_at')}")
