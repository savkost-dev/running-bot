"""Проверка частоты точек в Garmin: сколько записей отдаёт details по последней DD-активности.

Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/probe_garmin_stream.py <db_user_id>
Берёт сохранённый токен Garmin пользователя из БД (как garmin.py), bot.py не импортирует.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from garmin import _load_token, _build_client  # noqa: E402

uid = int(sys.argv[1])
token = _load_token(uid)
if not token:
    sys.exit(f"нет токена Garmin у user_id={uid}")
client = _build_client(token)

acts = client.get_activities(0, 5)
aid = next(a["activityId"] for a in acts if "DD_" in (a.get("activityName") or ""))
d = client.get_activity_details(aid, maxchart=20000)
m = d["activityDetailMetrics"]
print("activity", aid, "points", len(m), "measurements", d.get("measurementCount"))
keys = [x["key"] for x in d["metricDescriptors"]]
print("keys:", [k for k in keys if "Distance" in k or "Duration" in k or "Timestamp" in k])
first, last = m[0]["metrics"], m[-1]["metrics"]
print("first:", first)
print("last:", last)
