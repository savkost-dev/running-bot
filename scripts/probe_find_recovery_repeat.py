"""Поиск DD-тренировок, где в плане есть repeat-группа с recovery ВНУТРИ.

READ-ONLY. В базу НЕ пишет, боевые модули НЕ меняет. Нужен, чтобы найти сложную
тренировку (составной отрезок с отдыхом внутри серии) для проверки гипотезы о
нумерации wktStepIndex на втором, независимом кейсе.

Проходит активности Garmin с маской DD_ за указанный период (дефолт — май-июнь
2026), тянет их workout по workoutId и печатает те, где хотя бы одна
RepeatGroupDTO содержит recovery-шаг среди прямых детей. Для каждой совпавшей —
имя, дата, activityId, workoutId и краткую структуру плана.

Запуск (на сервере):
    venv/bin/python3 scripts/probe_find_recovery_repeat.py            # uid=2, 2026-05..06
    venv/bin/python3 scripts/probe_find_recovery_repeat.py 2 2026-05 2026-06
"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import garmin
import activity_review as ar


def _is_repeat(st):
    return st.get("type") == "RepeatGroupDTO" or \
        (st.get("stepType") or {}).get("stepTypeKey") == "repeat"


def _stype(st):
    return (st.get("stepType") or {}).get("stepTypeKey")


def _has_recovery_in_repeat(wkt):
    """True, если у какой-то RepeatGroupDTO среди прямых детей есть recovery/rest."""
    found = [False]

    def walk(steps):
        for st in steps or []:
            if not isinstance(st, dict):
                continue
            if _is_repeat(st):
                children = st.get("workoutSteps") or []
                if any(isinstance(c, dict) and _stype(c) in ("recovery", "rest")
                       for c in children):
                    found[0] = True
                walk(children)  # на случай вложенных групп
            # executable — пропускаем
    for seg in (wkt.get("workoutSegments") or []):
        walk(seg.get("workoutSteps"))
    return found[0]


def _structure(wkt):
    """Однострочная сводка структуры: 2x(600i/400i) + 2x(800i/200i) и т.п."""
    def part(steps):
        chunks = []
        for st in steps or []:
            if not isinstance(st, dict):
                continue
            if _is_repeat(st):
                it = st.get("numberOfIterations") or 1
                chunks.append(f"{it}x({part(st.get('workoutSteps'))})")
            else:
                d = st.get("endConditionValue")
                tag = {"interval": "i", "recovery": "r", "rest": "r",
                       "warmup": "w", "cooldown": "c"}.get(_stype(st), "?")
                chunks.append(f"{int(d) if d else '?'}{tag}")
        return " + ".join(chunks)
    parts = [part(seg.get("workoutSteps")) for seg in (wkt.get("workoutSegments") or [])]
    return " | ".join(p for p in parts if p)


async def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    p_from = sys.argv[2] if len(sys.argv) > 2 else "2026-05"
    p_to = sys.argv[3] if len(sys.argv) > 3 else "2026-06"

    client = await garmin._client(uid)
    if not client:
        print(f"Garmin не подключён для uid={uid}.")
        return

    # тянем побольше активностей, фильтруем DD_ + период по startTimeLocal
    acts = await asyncio.to_thread(client.get_activities, 0, 200)
    cands = []
    for a in (acts or []):
        name = str(a.get("activityName") or "")
        if "DD_" not in name and "DD-" not in name:
            continue
        start = str(a.get("startTimeLocal") or "")[:7]  # YYYY-MM
        if not (p_from <= start <= p_to):
            continue
        if not a.get("workoutId"):
            continue
        cands.append(a)

    print(f"Кандидатов DD_ за {p_from}..{p_to} с workoutId: {len(cands)}")
    matches = []
    for a in cands:
        wkt_id = a.get("workoutId")
        try:
            wkt = await asyncio.to_thread(client.get_workout_by_id, wkt_id)
        except Exception as e:
            print(f"  ! {a.get('activityName')}: workout {wkt_id} недоступен "
                  f"({type(e).__name__})")
            continue
        if _has_recovery_in_repeat(wkt):
            matches.append((a, wkt))

    print(f"\nСовпадений (recovery внутри repeat-группы): {len(matches)}")
    for a, wkt in matches:
        print("-" * 72)
        print(f"  name       : {a.get('activityName')}")
        print(f"  date       : {a.get('startTimeLocal')}")
        print(f"  activityId : {a.get('activityId')}")
        print(f"  workoutId  : {a.get('workoutId')}")
        print(f"  структура  : {_structure(wkt)}")
        flat = ar._flatten_plan_steps(wkt)
        print(f"  flatten idx: {[(s['idx'], s['stype'], int(s['dist']) if s['dist'] else None) for s in flat]}")


if __name__ == "__main__":
    asyncio.run(main())
