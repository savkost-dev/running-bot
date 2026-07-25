"""Пробник: какие поля лежат в workout_analysis.analyzed_json (локальная копия БД).
Запуск: python scripts\probe_s4_fields.py [YYYY-MM-DD]
Без аргумента — последние 3 валидных анализа."""
import json
import sqlite3
import sys

DB = r"D:\running-bot\data\running_bot.db"

date = sys.argv[1] if len(sys.argv) > 1 else None
q = ("SELECT workout_date, workout_type, analyzed_json FROM workout_analysis "
     "WHERE is_valid = 1 ")
args = ()
if date:
    q += "AND workout_date = ? "
    args = (date,)
q += "ORDER BY updated_at DESC LIMIT 3"

conn = sqlite3.connect(DB)
for wdate, wtype, raw in conn.execute(q, args):
    try:
        d = json.loads(raw)
    except Exception as e:
        print(f"{wdate} {wtype}: JSON не парсится ({e})")
        continue
    print(f"=== {wdate} ({wtype}) ===")
    print("ключи:", ", ".join(d.keys()))
    for k in ("summary", "overall_purpose", "workout_summary", "purpose", "structure"):
        v = d.get(k)
        if v is not None:
            s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            print(f"  {k}: {s[:200]}{'…' if len(s) > 200 else ''}")
    print()
conn.close()
