"""Пересобрать эталоны (workout_templates) за дату из сохранённого анализа Шага 1.

Зачем: шаблоны строятся при сохранении анализа; если генератор (fit_generator) исправили позже,
старые шаблоны в базе остаются прежними. Повторяет логику bot._save_workout_templates, bot.py не импортирует.
Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/rebuild_templates.py 2026-09-11
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import database as db  # noqa: E402
from fit_generator import build_garmin_from_analysis  # noqa: E402

date = sys.argv[1]
with db.get_connection() as conn:
    row = conn.execute(
        "SELECT analyzed_json FROM workout_analysis WHERE workout_date=? AND is_valid=1 "
        "ORDER BY updated_at DESC LIMIT 1", (date,)).fetchone()
if not row or not row[0]:
    sys.exit("анализ за дату не найден")
parsed = json.loads(row[0])
saved = 0
for g in (parsed.get("groups") or []):
    gnum = str(g.get("number") or "").strip()
    if not gnum or "здоров" in gnum.lower():
        continue
    wj = build_garmin_from_analysis(parsed, gnum)
    db.save_workout_template(date, gnum, "interval", json.dumps(wj, ensure_ascii=False))
    saved += 1
    steps = [s for s in wj.get("workoutSegments", [{}])[0].get("workoutSteps", [])]
    print(f"группа {gnum}: сохранено, шагов верхнего уровня {len(steps)}")
    # Контроль: целевые скорости по шагам (включая вложенные).
    def walk(ss, depth=1):
        for s in ss:
            if s.get("workoutSteps"):
                walk(s["workoutSteps"], depth + 1)
            else:
                print("  " * depth + f"{s.get('stepType', {}).get('stepTypeKey')} "
                      f"{s.get('endConditionValue')} m  target {s.get('targetValueOne')}..{s.get('targetValueTwo')}")
    walk(steps)
print(f"итого групп: {saved}")
