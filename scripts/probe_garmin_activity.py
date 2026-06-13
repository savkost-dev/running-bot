"""Пробник Garmin: последняя беговая активность + лэпы + HR-зоны — read-only.

Что делает: через garmin._client (живой токен/реавторизация) берёт список
последних активностей, выбирает последнюю беговую, печатает:
  - сводку активности (ключи и основные поля),
  - лэпы (splits): ключи первого лэпа и таблицу idx/тип/дистанция/время/темп/HR,
  - время в пульсовых зонах (hrTimeInZones),
  - какие методы деталей доступны (для оценки посекундных рядов).
Цель: понять реальную структуру данных для будущего слоя s4_activity.

Пишет в базу: НЕТ (read-only; может обновить токен Garmin при реавторизации — штатно).
Импортирует: garmin, database. НЕ импортирует bot.py.

Запуск:
    venv/bin/python3 scripts/probe_garmin_activity.py        # uid=2 (Anton)
    venv/bin/python3 scripts/probe_garmin_activity.py 4      # другой db_user_id
"""
import sys
import os
import json
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import garmin


def _pace(dist_m, dur_s):
    if not dist_m or not dur_s:
        return "—"
    sec_per_km = dur_s / (dist_m / 1000)
    return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}"


async def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    client = await garmin._client(uid)
    if not client:
        print(f"user={uid}: нет клиента Garmin")
        return

    acts = await asyncio.to_thread(client.get_activities, 0, 20)
    acts = acts or []
    print("Последние активности:")
    for a in acts[:10]:
        print(f"  {a.get('startTimeLocal')}  {(a.get('activityType') or {}).get('typeKey')}"
              f"  {a.get('activityName')!r}")
    # Наши тренировки помечены маской DD_YYYYMMDD-<группа>_lvl (workout_filename) —
    # активность, выполненная по загруженной тренировке, наследует имя.
    # Без фолбэков: нет DD-активности — честно выходим.
    runs = [a for a in acts
            if "running" in str((a.get("activityType") or {}).get("typeKey", ""))
            and "DD_" in str(a.get("activityName") or "")]
    if not runs:
        print("\nБеговой активности с маской DD_ в последних 20 нет.")
        return
    act = runs[0]
    act_id = act.get("activityId")
    print(f"=== Активность {act_id}: {act.get('activityName')} "
          f"{act.get('startTimeLocal')} ===")
    print(f"  дистанция={act.get('distance')}м  время={act.get('duration')}с  "
          f"avgHR={act.get('averageHR')}  maxHR={act.get('maxHR')}")
    print(f"  ключи активности: {sorted(act.keys())}")

    # Лэпы
    try:
        splits = await asyncio.to_thread(client.get_activity_splits, act_id)
    except Exception as e:
        print(f"get_activity_splits: ошибка {type(e).__name__}: {e}")
        splits = None
    if isinstance(splits, dict):
        laps = splits.get("lapDTOs") or splits.get("laps") or []
        print(f"\n=== Лэпы: {len(laps)} (ключи ответа: {list(splits.keys())}) ===")
        if laps:
            print(f"  ключи лэпа: {sorted(laps[0].keys())}")
            for i, lp in enumerate(laps, 1):
                d = lp.get("distance")
                t = lp.get("duration") or lp.get("movingDuration")
                itype = lp.get("intensityType")
                print(f"  {i:>2}. {str(itype):<10} {d or 0:>7.0f}м "
                      f"{t or 0:>7.1f}с  темп {_pace(d, t):>5}  "
                      f"HR {lp.get('averageHR')}/{lp.get('maxHR')}")
    else:
        print(f"splits: {type(splits).__name__} = {str(splits)[:200]}")

    # Время в пульсовых зонах
    try:
        zones = await asyncio.to_thread(client.get_activity_hr_in_timezones, act_id)
        print(f"\n=== HR-зоны ===")
        for z in (zones or []):
            print(f"  Z{z.get('zoneNumber')}: {z.get('secsInZone', 0):.0f}с "
                  f"(низ {z.get('zoneLowBoundary')})")
    except Exception as e:
        print(f"get_activity_hr_in_timezones: ошибка {type(e).__name__}: {e}")

    # Какие ещё методы деталей есть у клиента (для посекундных рядов)
    detail_methods = [m for m in dir(client)
                      if "activity" in m.lower() and not m.startswith("_")]
    print(f"\nДоступные activity-методы клиента:\n  " + "\n  ".join(sorted(detail_methods)))


if __name__ == "__main__":
    asyncio.run(main())
