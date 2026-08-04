"""Что лежит в workout_templates: свежесть эталонов и темпы базового шага.

Показывает последние записи и, для указанной даты, разбирает сохранённый
Garmin-JSON: дистанции/темпы первых шагов — чтобы увидеть, старый эталон
в базе или обновлённый.

Запуск (на сервере): venv/bin/python3 scripts/probe_templates.py [YYYY-MM-DD]
"""
import json
import sqlite3
import sys

date = sys.argv[1] if len(sys.argv) > 1 else None
conn = sqlite3.connect("running_bot.db")
cols = [r[1] for r in conn.execute("PRAGMA table_info(workout_templates)")]
print("колонки:", cols)

rows = conn.execute("SELECT * FROM workout_templates ORDER BY rowid DESC LIMIT 12").fetchall()
print("\nпоследние 12 записей:")
for r in rows:
    d = dict(zip(cols, r))
    short = {k: (f"<{len(str(v))} симв>" if isinstance(v, str) and len(str(v)) > 60 else v)
             for k, v in d.items()}
    print("  ", short)


def _pace(v):
    """м/с → мин:сек/км"""
    try:
        total = int(round(1000.0 / float(v)))
        return f"{total // 60}:{total % 60:02d}"
    except Exception:
        return "—"


if date:
    got = conn.execute(
        "SELECT * FROM workout_templates WHERE workout_date = ?", (date,)).fetchall()
    print(f"\nзаписей за {date}: {len(got)}")
    for r in got:
        d = dict(zip(cols, r))
        jcol = next((k for k in d if k.endswith("json")), None)
        try:
            wkt = json.loads(d.get(jcol) or "{}")
        except Exception as e:
            print("   не распарсился JSON:", e)
            continue
        steps = []
        for seg in wkt.get("workoutSegments") or []:
            for st in seg.get("workoutSteps") or []:
                for ch in ([st] if st.get("type") == "ExecutableStepDTO"
                           else (st.get("workoutSteps") or [])):
                    steps.append(ch)
        print(f"\n  группа {d.get('group_num') or d.get('group')} | "
              f"имя: {wkt.get('workoutName')} | шагов: {len(steps)}")
        for ch in steps[:4]:
            print(f"    {ch.get('endConditionValue')}м  "
                  f"{_pace(ch.get('targetValueOne'))}→{_pace(ch.get('targetValueTwo'))}")
conn.close()
