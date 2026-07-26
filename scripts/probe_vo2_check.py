"""Проверка ночного джоба VO2max v2: заполнение vo2max_device.
Запуск (на сервере): venv/bin/python3 scripts/probe_vo2_check.py running_bot.db"""
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "running_bot.db"
conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT user_id, vo2max, vo2max_source, vo2max_device, vo2max_device_source, "
    "vo2max_device_at, vo2max_manual, vo2max_priority "
    "FROM user_profile "
    "WHERE vo2max IS NOT NULL OR vo2max_device IS NOT NULL OR vo2max_manual IS NOT NULL "
    "ORDER BY user_id"
).fetchall()
fresh = 0
for uid, v, src, dv, dsrc, dat, mv, prio in rows:
    is_fresh = bool(dat and dat >= "2026-07-26")
    fresh += is_fresh
    mark = "🟢" if is_fresh else "  "
    print(f"{mark} uid={uid:3d} old={v} ({src}) | device={dv} ({dsrc}, {dat}) | "
          f"manual={mv} | prio={prio or 'device'}")
print(f"\nвсего: {len(rows)}, device обновлён сегодня: {fresh}")
conn.close()
