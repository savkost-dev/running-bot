"""Разовая уборка: старые эталоны группы «здоровье» в workout_templates.

С 04.08.2026 эталон для группы «здоровье» не создаётся (у неё нет темпов,
шаг получался бессмысленным: 1000 м без цели). Ранее сохранённые строки
остаются в базе — этот скрипт их показывает и по --apply удаляет.

Запуск (просмотр): venv/bin/python3 scripts/cleanup_health_templates.py
Удалить:           venv/bin/python3 scripts/cleanup_health_templates.py --apply
"""
import sqlite3
import sys

APPLY = "--apply" in sys.argv
conn = sqlite3.connect("running_bot.db")
rows = conn.execute(
    "SELECT workout_date, group_number, created_at FROM workout_templates "
    "WHERE lower(group_number) LIKE '%здоров%' ORDER BY workout_date DESC"
).fetchall()
print(f"строк группы «здоровье»: {len(rows)}")
for r in rows:
    print("  ", dict(zip(("дата", "группа", "создано"), r)))
if APPLY and rows:
    conn.execute("DELETE FROM workout_templates WHERE lower(group_number) LIKE '%здоров%'")
    conn.commit()
    print("Удалено.")
elif rows:
    print("Просмотр без записи. Для удаления добавь --apply")
conn.close()
