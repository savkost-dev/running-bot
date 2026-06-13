"""Показ плана тренировки из workout_analysis — read-only, для шага 2 (привязка факта к плану).

Что делает: по workout_date достаёт analyzed_json из workout_analysis,
печатает structure и плановые темпы запрошенной группы (по её number).
Цель: увидеть формат плана, чтобы строить матчинг факт↔план не вслепую.

Пишет в базу: НЕТ (read-only).
Импортирует: только sqlite3 напрямую к БД. НЕ импортирует bot.py.

Запуск:
    venv/bin/python3 scripts/show_plan.py 2026-06-12 3.5     # дата + группа
    venv/bin/python3 scripts/show_plan.py 2026-06-12         # все группы дня
"""
import sys
import os
import json
import sqlite3

DB = os.path.join(os.path.dirname(__file__), "..", "running_bot.db")


def main():
    if len(sys.argv) < 2:
        print("Укажи дату: show_plan.py 2026-06-12 [группа]")
        return
    wdate = sys.argv[1]
    want_group = sys.argv[2] if len(sys.argv) > 2 else None

    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT post_id, workout_type, is_valid, analyzed_json, extra_groups_json, "
        "analysis_mode, created_at FROM workout_analysis WHERE workout_date=? "
        "ORDER BY created_at DESC", (wdate,)).fetchall()
    if not rows:
        print(f"Нет записей workout_analysis за {wdate}")
        return

    print(f"Записей за {wdate}: {len(rows)}")
    post_id, wtype, valid, ajson, extra, mode, created = rows[0]
    print(f"Берём свежую: post_id={post_id} type={wtype} valid={valid} "
          f"mode={mode} created={created}")
    if len(rows) > 1:
        print(f"(есть ещё {len(rows)-1} записей за эту дату — взята последняя)")

    if not ajson:
        print("analyzed_json пуст")
        return
    data = json.loads(ajson)

    print("\n=== summary ===")
    print(data.get("summary"))

    print("\n=== structure ===")
    for b in data.get("structure") or []:
        print(json.dumps(b, ensure_ascii=False))

    print("\n=== groups ===")
    groups = data.get("groups") or []
    for g in groups:
        num = str(g.get("number"))
        if want_group and num != str(want_group):
            continue
        print(f"\n--- Группа {num} (health={g.get('health_group')}) ---")
        for blk in g.get("blocks") or []:
            print(json.dumps(blk, ensure_ascii=False))

    if want_group and not any(str(g.get("number")) == str(want_group) for g in groups):
        print(f"\n[!] Группа {want_group} не найдена. Есть: "
              f"{[str(g.get('number')) for g in groups]}")


if __name__ == "__main__":
    main()
