"""Что лежит в blocks групп 3 и 3.5 анализа 2026-08-04 (первоисточник темпов воркаута).
Запуск (на сервере): venv/bin/python3 scripts/probe_s4_blocks.py"""
import json
import sqlite3

conn = sqlite3.connect("running_bot.db")
row = conn.execute(
    "SELECT analyzed_json FROM workout_analysis WHERE workout_date='2026-08-04' "
    "AND is_valid=1 ORDER BY updated_at DESC LIMIT 1").fetchone()
d = json.loads(row[0])
print("=== structure ===")
print(json.dumps(d.get("structure"), ensure_ascii=False, indent=1))
for g in d.get("groups") or []:
    if str(g.get("number")) in ("3", "3.5"):
        print(f"\n=== группа {g.get('number')} ===")
        print(json.dumps(g.get("blocks"), ensure_ascii=False, indent=1))
conn.close()
