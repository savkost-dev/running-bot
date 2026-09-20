"""probe_long_groups.py — разведка (20.09.2026): как в анализе анонса лонга хранятся группы.

Только читает базу. Печатает для workout_analysis с workout_type='long' на заданную дату
ключи анализа, поле groups и structure (JSON, с отступами).

Запуск на сервере:  cd /opt/running-bot && venv/bin/python scripts/probe_long_groups.py 2026-09-20
bot.py не импортирует.
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "running_bot.db"


def main(wdate):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, post_id, workout_date, workout_type, is_valid, analyzed_json "
        "FROM workout_analysis WHERE workout_type = 'long' AND workout_date = ? ORDER BY id DESC",
        (wdate,)).fetchall()
    if not rows:
        print(f"Нет анализа лонга за {wdate}")
        return
    for r in rows:
        print(f"=== id={r['id']} post={r['post_id']} дата={r['workout_date']} valid={r['is_valid']}")
        a = json.loads(r["analyzed_json"] or "{}")
        print("ключи анализа:", list(a.keys()))
        print("--- groups:")
        print(json.dumps(a.get("groups"), ensure_ascii=False, indent=1))
        print("--- structure:")
        print(json.dumps(a.get("structure"), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2026-09-20")
