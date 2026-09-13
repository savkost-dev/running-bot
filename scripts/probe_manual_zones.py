"""Кто подключил трекер, но зоны считаются от РУЧНОГО якоря (ЛП или VO2max, введённых в профиле).

Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/probe_manual_zones.py
Печатает: имя, uid, сервисы, источник зон. Ничего не меняет.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import database as db  # noqa: E402
import zones  # noqa: E402

SERVICES = ("garmin", "coros", "coros_mcp", "strava", "whoop", "polar")
tracker = {}
for svc in SERVICES:
    try:
        for tg_id, name, uname in db.get_users_with_service_full(svc):
            tracker.setdefault(tg_id, []).append(svc)
    except Exception as e:  # noqa: BLE001
        print(f"{svc}: {type(e).__name__}: {e}")

rows = []
for tid, name, uname, has in db.get_all_users_with_status():
    if not has or tid not in tracker:
        continue
    uid = db.get_or_create_user(tid, name)
    z = zones.get_pace_zones(uid) or {}
    src = str(z.get("zones_source") or z.get("source") or "")
    if "manual" in src.lower():
        rows.append((name, uname, uid, ",".join(tracker[tid]), src, z.get("anchor") or z.get("lt_pace") or ""))

print(f"с трекером и ручным якорем зон: {len(rows)}")
for name, uname, uid, svcs, src, anchor in sorted(rows, key=lambda r: str(r[0])):
    print(f"  uid={uid:<4} {str(name)[:22]:<22} @{uname or '-':<18} {svcs:<18} {src}  {anchor}")
