"""Что даёт трекер Garmin пользователю (VO2max, лактатный порог) — против ручных значений в профиле.

Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/probe_garmin_anchor.py <uid>
Ничего не меняет.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import database as db  # noqa: E402
import garmin  # noqa: E402


async def main():
    uid = int(sys.argv[1])
    prof = db.get_user_profile(uid) or {}
    print("профиль (ручное):", {k: prof.get(k) for k in prof if any(s in k for s in ("vo2", "lt_", "lactate", "manual"))})
    try:
        print("Garmin VO2max:", await garmin.get_vo2max(uid))
    except Exception as e:  # noqa: BLE001
        print("Garmin VO2max: ошибка", type(e).__name__, e)
    try:
        print("Garmin ЛП:", await garmin.get_lactate_threshold(uid))
    except Exception as e:  # noqa: BLE001
        print("Garmin ЛП: ошибка", type(e).__name__, e)


asyncio.run(main())
