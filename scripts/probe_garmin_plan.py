"""Пробник плана тренировки из Garmin по workoutId — read-only, для шага 2.

Что делает: берёт активность (с маской DD_ или по activity_id), достаёт её
workoutId, тянет workout по нему через клиент Garmin и печатает плановые
шаги с целевыми темпами (pace.zone targetValueOne/Two в м/с → мин/км).
Цель: подтвердить, что эталон можно брать из Garmin, и увидеть формат.

Пишет в базу: НЕТ (read-only; реавторизация токена — штатно).
Импортирует: garmin. НЕ импортирует bot.py.

Запуск:
    venv/bin/python3 scripts/probe_garmin_plan.py            # uid=2, последняя DD-активность
    venv/bin/python3 scripts/probe_garmin_plan.py 2 23219097987  # конкретная активность
"""
import sys
import os
import json
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import garmin


def _ms_to_pace(v):
    """м/с → 'мин:сек/км'. None если нет/0."""
    if not v:
        return None
    sec_per_km = 1000.0 / v
    return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}"


def _walk_steps(steps, depth=0):
    """Рекурсивно печатает шаги workout (включая вложенные в repeat)."""
    for st in steps or []:
        if not isinstance(st, dict):
            continue
        stype = (st.get("stepType") or {}).get("stepTypeKey")
        if st.get("type") == "RepeatGroupDTO" or stype == "repeat":
            iters = st.get("numberOfIterations") or st.get("endConditionValue")
            print(f"{'  '*depth}REPEAT ×{iters}:")
            _walk_steps(st.get("workoutSteps"), depth + 1)
            continue
        end_val = st.get("endConditionValue")
        end_key = (st.get("endCondition") or {}).get("conditionTypeKey")
        ttype = (st.get("targetType") or {}).get("workoutTargetTypeKey")
        t1 = st.get("targetValueOne")
        t2 = st.get("targetValueTwo")
        pace_str = ""
        if ttype == "pace.zone":
            pace_str = f"  темп {_ms_to_pace(t1)}…{_ms_to_pace(t2)} (м/с {t1}/{t2})"
        print(f"{'  '*depth}{stype:<10} {end_key}={end_val}  target={ttype}{pace_str}")


async def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    forced_id = int(sys.argv[2]) if len(sys.argv) > 2 else None

    client = await garmin._client(uid)
    if not client:
        print(f"user={uid}: нет клиента Garmin")
        return

    if forced_id:
        act = await asyncio.to_thread(client.get_activity, forced_id)
    else:
        acts = await asyncio.to_thread(client.get_activities, 0, 20)
        runs = [a for a in (acts or [])
                if "running" in str((a.get("activityType") or {}).get("typeKey", ""))
                and "DD_" in str(a.get("activityName") or "")]
        if not runs:
            print("Беговой активности с маской DD_ в последних 20 нет")
            return
        act = runs[0]

    name = act.get("activityName")
    wkt_id = act.get("workoutId")
    print(f"Активность: {name!r}  activityId={act.get('activityId')}")
    print(f"workoutId = {wkt_id!r}")
    if not wkt_id:
        print("\n[!] workoutId пуст — активность не привязана к загруженной тренировке.")
        print("    Значит эталон берём из нашей БД (workout_analysis). Это нормальный фолбэк.")
        return

    # Тянем workout по id
    method = None
    for m in ("get_workout_by_id", "get_workout"):
        if hasattr(client, m):
            method = m
            break
    if not method:
        cand = [m for m in dir(client) if "workout" in m.lower() and not m.startswith("_")]
        print(f"\n[!] Нет метода получения workout. Кандидаты: {cand}")
        return

    try:
        wkt = await asyncio.to_thread(getattr(client, method), wkt_id)
    except Exception as e:
        print(f"\n{method}({wkt_id}): ошибка {type(e).__name__}: {e}")
        return

    print(f"\n=== Workout '{wkt.get('workoutName')}' (через {method}) ===")
    segs = wkt.get("workoutSegments") or []
    for seg in segs:
        _walk_steps(seg.get("workoutSteps"))

    print("\n=== Сырой JSON workout (первые 2000 символов) ===")
    print(json.dumps(wkt, ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    asyncio.run(main())
