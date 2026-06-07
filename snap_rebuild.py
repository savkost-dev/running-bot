"""Разовое пересоздание утренних снимков по всем (как опросник, но без окна).
Снимок строится из УЖЕ лежащего сырья (слой 1). set_morning_caught на сегодня."""
import sys
sys.path.insert(0, '/opt/running-bot/src')
from datetime import datetime, timezone, timedelta
from database import get_all_users, get_or_create_user, set_morning_caught
from bot import _night_services, _night_ready, _collect_morning_snapshot

MSK = timezone(timedelta(hours=3))
today = datetime.now(MSK).strftime("%Y-%m-%d")

built = skipped = 0
for telegram_id, name, _ in get_all_users():
    uid = get_or_create_user(telegram_id, name)
    if not _night_services(uid):
        continue
    ready = _night_ready(uid, today)
    snap = _collect_morning_snapshot(uid)
    set_morning_caught(uid, today, snapshot=snap)
    built += 1
    print(f"uid {uid:>3} {name:<16} ready={ready} "
          f"TR={snap.get('tr')} BB={snap.get('bb')} HRV={snap.get('hrv')} "
          f"RHR={snap.get('rhr')} sleep={snap.get('sleep_h')} wake={snap.get('wake_at')}")

print(f"\nГотово: собрано снимков={built} (дата {today})")
