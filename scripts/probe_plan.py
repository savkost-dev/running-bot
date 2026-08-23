"""Показать эталон тренировки за дату: что бот записал в анализ и в шаблоны.

Запуск:  venv/bin/python3 scripts/probe_plan.py 2026-08-18 [номер_группы]

Без номера группы печатает структуру задания и список групп,
с номером — полный шаблон именно этой группы (то, с чем сверяется разбор).
"""
import sys
import json

sys.path.insert(0, "/opt/running-bot/src")

from database import get_connection  # noqa: E402

date = sys.argv[1] if len(sys.argv) > 1 else None
group = sys.argv[2] if len(sys.argv) > 2 else None

if not date:
    print("Формат: probe_plan.py 2026-08-18 [номер группы]")
    raise SystemExit(1)

with get_connection() as conn:
    rows = conn.execute(
        "SELECT post_id, workout_date, workout_type, is_valid, analyzed_json "
        "FROM workout_analysis WHERE workout_date = ?", (date,)).fetchall()

    if not rows:
        print(f"Анализа за {date} нет.")
        raise SystemExit(0)

    for post_id, wdate, wtype, valid, ajson in rows:
        print(f"=== анализ: пост {post_id} · {wdate} · тип {wtype} · валиден {valid}")
        try:
            a = json.loads(ajson or "{}")
        except Exception as e:
            print("  не разобрать:", e)
            continue

        print("  суть:", (a.get("summary") or "—")[:200])
        print("  структура:")
        for b in (a.get("structure") or []):
            print("   ", b)
        print("  группы:")
        for g in (a.get("groups") or []):
            num = str(g.get("number"))
            if group and num != str(group):
                continue
            print(f"    группа {num}:")
            print("      ", json.dumps(g, ensure_ascii=False)[:600])

    # шаблоны (эталоны), с которыми сверяется разбор
    try:
        tpl = conn.execute(
            "SELECT * FROM workout_templates WHERE workout_date = ?", (date,)).fetchall()
    except Exception as e:
        print("\nТаблицы шаблонов нет или другое имя:", e)
        tpl = []

    if tpl:
        print(f"\n=== эталоны в базе: {len(tpl)} шт.")
        cols = [d[0] for d in conn.execute(
            "SELECT * FROM workout_templates LIMIT 1").description]
        for r in tpl:
            d = dict(zip(cols, r))
            num = str(d.get("group_number") or d.get("group") or "")
            if group and num != str(group):
                continue
            print(f"\n  группа {num}:")
            for k, v in d.items():
                s = str(v)
                print(f"    {k}: {s[:500]}")
