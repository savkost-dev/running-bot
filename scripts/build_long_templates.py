"""Собрать постоянную библиотеку эталонов лонга и положить в workout_templates.

12 строк: группы 1-6 × (ровно / с ускорением «+»), workout_date = 2999-12-31, wtype = long.
Повторный запуск перезаписывает. Раз/зам в эталон не кладём — добавляются галочкой при загрузке.

Запуск на сервере:  venv/bin/python scripts/build_long_templates.py [--dry]
Локально (проверка без записи): python scripts\\build_long_templates.py --dry
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from fit_generator import (LONG_GROUP_PACES, LONG_TEMPLATE_DATE,  # noqa: E402
                           build_long_template, long_template_name)

DB = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "running_bot.db"))


def main() -> None:
    dry = "--dry" in sys.argv
    rows = []
    for grp in LONG_GROUP_PACES:
        for progressive in (False, True):
            wj = build_long_template(grp, progressive)
            key = f"{grp}+" if progressive else grp
            rows.append((LONG_TEMPLATE_DATE, key, "long", json.dumps(wj, ensure_ascii=False)))
            steps = wj["workoutSegments"][0]["workoutSteps"]
            print(f"{long_template_name(grp, progressive):<10} шагов {len(steps)}  "
                  + "  ".join(f"{s['endConditionValue']/60:.0f}мин@{s['targetValueOne']:.3f}м/с"
                              for s in steps))
    if dry:
        print(f"\n--dry: {len(rows)} эталонов, в базу не пишу ({DB})")
        return
    conn = sqlite3.connect(DB)
    conn.executemany(
        "INSERT OR REPLACE INTO workout_templates (workout_date, group_number, wtype, workout_json) "
        "VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM workout_templates WHERE workout_date = ?",
                     (LONG_TEMPLATE_DATE,)).fetchone()[0]
    print(f"\nЗаписано {len(rows)}, в базе эталонов лонга: {n}")


if __name__ == "__main__":
    main()
