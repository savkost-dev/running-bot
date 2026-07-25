"""Печать кандидатов Garmin и Strava для DD-активности по селектору.
Запуск (на сервере): venv/bin/python3 scripts/probe_find_activity.py <db_user_id> <selector>
Пример: ... 2 DD_20260707
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import ai_package  # noqa: E402


async def main():
    uid = int(sys.argv[1])
    sel = sys.argv[2] if len(sys.argv) > 2 else None
    for label, fn in (("garmin", ai_package._garmin_candidate),
                      ("strava", ai_package._strava_candidate)):
        try:
            cand = await fn(uid, sel)
        except Exception as e:
            print(f"{label}: ошибка ({e})")
            continue
        if not cand:
            print(f"{label}: не найдено")
            continue
        laps = (cand["splits"].get("lapDTOs") or []) if isinstance(cand.get("splits"), dict) else []
        print(f"{label}: {cand['name']} | {cand['wdate']} | группа {cand['wgroup']} | "
              f"лэпов {len(laps)} | шагов плана {len(cand.get('plan_steps') or [])}")


asyncio.run(main())
