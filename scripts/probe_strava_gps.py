"""Есть ли в Strava-активности признаки «без спутников».

Печатает по последней DD-пробежке поля, по которым можно отличить запись без GPS:
start_latlng / end_latlng / map.summary_polyline / device_name / trainer / manual,
плюс дистанцию и число лэпов для сверки.

Запуск (на сервере): venv/bin/python3 scripts/probe_strava_gps.py [db_user_id]
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import strava  # noqa: E402

UID = int(sys.argv[1]) if len(sys.argv) > 1 else 2
KEYS = ("id", "name", "type", "sport_type", "start_date_local", "distance",
        "start_latlng", "end_latlng", "device_name", "trainer", "manual",
        "elev_high", "elev_low", "total_elevation_gain", "average_speed")


async def main():
    token = await strava.ensure_valid_token(UID)
    if not token:
        print("нет токена")
        return
    acts = await strava.get_recent_activities(token, days=30)
    runs = [a for a in acts
            if a.get("type") == "Run" and re.search(r"DD[-_]", str(a.get("name") or ""))]
    runs.sort(key=lambda a: str(a.get("start_date_local") or ""), reverse=True)
    if not runs:
        print("DD-пробежек нет")
        return
    for a in runs[:3]:
        print("=" * 60)
        for k in KEYS:
            print(f"  {k}: {a.get(k)}")
        m = a.get("map") or {}
        poly = m.get("summary_polyline")
        print(f"  map.summary_polyline: {'ПУСТО' if not poly else str(poly)[:40] + '...'}")
        print(f"  (все ключи summary: {sorted(a.keys())})" if a is runs[0] else "")
        detail = await strava.get_activity_detail(token, a.get("id"))
        if detail and a is runs[0]:
            d = {k: detail.get(k) for k in
                 ("device_name", "start_latlng", "manual", "trainer", "elapsed_time")}
            print(f"  DETAIL: {json.dumps(d, ensure_ascii=False)}")


asyncio.run(main())
