"""Лэпы Garmin-активности: wktStepIndex, intensity, dist, dur + план.
Запуск (на сервере): venv/bin/python3 scripts/probe_laps.py <db_user_id> <selector>
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import ai_package  # noqa: E402
import activity_review as ar  # noqa: E402


async def main():
    uid, sel = int(sys.argv[1]), sys.argv[2]
    cand = await ai_package._garmin_candidate(uid, sel)
    if not cand:
        print("garmin: не найдено")
        return
    print(f"{cand['name']} | {cand['wdate']} | группа {cand['wgroup']}")
    ps = cand.get("plan_steps") or []
    print(f"план: {len(ps)} шагов")
    for s in ps:
        print(f"  idx={s['idx']} {s['stype']} dist={s.get('dist')} bounds={s.get('bounds')}")
    laps = (cand["splits"].get("lapDTOs") or [])
    print(f"лэпов: {len(laps)}")
    for i, l in enumerate(laps, 1):
        d = l.get("distance")
        t = l.get("duration") or l.get("movingDuration")
        pace = ar._pace_formatter(t / (d / 1000)) if (d and t) else "—"
        print(f"  {i:2d}. wktStepIdx={l.get('wktStepIndex')} {str(l.get('intensityType')):8s} "
              f"dist={d} dur={t} pace={pace}")


asyncio.run(main())
