"""Разовое заполнение workout_templates за сегодняшний интервальный анонс.

Берёт последний валидный interval-анализ (Шаг 1) из workout_analysis,
прогоняет группы 1,2,3,3.5,4,5 через build_garmin_from_analysis и пишет
готовый Garmin JSON в workout_templates (тот же код, что в вечерней рассылке).
Нужен, чтобы не ждать следующей рассылки.

Пишет в базу: ДА (workout_templates, upsert по date+group+wtype).
Импортирует: database, fit_generator. НЕ импортирует bot.py.

Запуск:
    /opt/running-bot/venv/bin/python3 scripts/fill_templates_today.py
"""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import get_latest_workout_analysis, save_workout_template
from fit_generator import build_garmin_from_analysis

GROUPS = ["1", "2", "3", "3.5", "4", "5"]


def main():
    row, status = get_latest_workout_analysis("interval")
    if status == "empty" or row is None:
        print("Нет валидного interval-анализа в базе.")
        return
    parsed = json.loads(row.get("analyzed_json") or "{}")
    date = parsed.get("workout_date") or row.get("workout_date")
    if not date:
        print("Нет workout_date в анализе.")
        return
    print(f"Анализ на {date}, статус={status}")

    saved = 0
    for gnum in GROUPS:
        try:
            wj = build_garmin_from_analysis(parsed, gnum)
            save_workout_template(date, gnum, "interval",
                                  json.dumps(wj, ensure_ascii=False))
            print(f"  группа {gnum}: OK")
            saved += 1
        except Exception as e:
            print(f"  группа {gnum}: пропущена ({type(e).__name__}: {e})")
    print(f"Сохранено эталонов: {saved} на {date}")


if __name__ == "__main__":
    main()
