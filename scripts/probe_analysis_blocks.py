"""Диагностика блоков анализа: по дате печатает structure (purpose/description/role/дистанции)
и темпы группы по блокам — чтобы понять, почему отрезок «на максимум» ушёл без темпа.

Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/probe_analysis_blocks.py 2026-09-11 3.5
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import database as db  # noqa: E402

date, group = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
with db.get_connection() as conn:
    row = conn.execute(
        "SELECT analyzed_json FROM workout_analysis WHERE workout_date=? AND is_valid=1 "
        "ORDER BY updated_at DESC LIMIT 1", (date,)).fetchone()
if not row:
    sys.exit("анализ не найден")
a = json.loads(row[0])
print("top keys:", sorted(a.keys()))
for i, b in enumerate(a.get("structure") or a.get("blocks") or [], 1):
    print(f"\n[structure {i}]", json.dumps(b, ensure_ascii=False))
groups = a.get("groups") or {}
if isinstance(groups, dict):
    for k, v in groups.items():
        if group is None or str(k) == group:
            print(f"\n[group {k}]", json.dumps(v, ensure_ascii=False)[:2000])
else:
    print("\n[groups]", json.dumps(groups, ensure_ascii=False)[:3000])
