"""Проверка таблицы workout_templates — read-only.

Печатает: число строк и список (дата, группа, тип, длина JSON, created_at).
Полный workout_json не выводит — он большой; при необходимости можно
посмотреть отдельно по ключу.

Пишет в базу: НЕТ (read-only).
Импортирует: только sqlite3. НЕ импортирует bot.py.

Запуск на сервере:
    /opt/running-bot/venv/bin/python3 scripts/check_templates.py
"""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "running_bot.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='workout_templates'"
        ).fetchone()
        if not exists:
            print("Таблица workout_templates НЕ найдена.")
            return

        total = conn.execute("SELECT COUNT(*) FROM workout_templates").fetchone()[0]
        print(f"workout_templates: строк = {total}")

        rows = conn.execute(
            "SELECT workout_date, group_number, wtype, length(workout_json), created_at "
            "FROM workout_templates ORDER BY workout_date DESC, group_number"
        ).fetchall()
        if not rows:
            print("(записей нет)")
            return
        print(f"{'date':<11} {'grp':>4} {'type':<9} {'json_len':>8} created_at")
        for date, grp, wtype, jlen, created in rows:
            print(f"{str(date):<11} {str(grp):>4} {str(wtype):<9} {jlen:>8} {created}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
