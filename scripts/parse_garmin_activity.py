"""Парсер Garmin-активности в единый слой s4_activity — ШАГ 1 (read-only, без записи в БД).

Что делает: берёт последнюю беговую активность с маской DD_ в названии
(наша тренировка), забирает сводку + лэпы (splits) + HR-зоны и нормализует
в единый dict s4_activity. Печатает результат как JSON + читаемую таблицу.
НА ЭТОЙ ИТЕРАЦИИ В БД НЕ ПИШЕТ — сначала смотрим качество разбора.

Слой s4_activity:
  source, activity_id, date, name, workout_date, group (из DD-маски)
  summary: distance_m, duration_s, avg_hr, max_hr, training_load
  hr_zones: [{zone, secs, low_boundary}]
  laps: [{idx, role: work|rest|tail|unknown, distance_m, duration_s,
          avg_pace_s, avg_hr, max_hr, wkt_step_index, compliance_score,
          pace_evenness}]

Правило данных: никаких молчаливых подстановок. Нет intensityType → role=unknown.
tail = аномально короткий последний ACTIVE (<50% медианной рабочей дистанции).

Пишет в базу: НЕТ. Может обновить токен Garmin при реавторизации — штатно.
Импортирует: garmin. НЕ импортирует bot.py.

Запуск:
    venv/bin/python3 scripts/parse_garmin_activity.py            # uid=2 (Anton)
    venv/bin/python3 scripts/parse_garmin_activity.py 4          # другой db_user_id
    venv/bin/python3 scripts/parse_garmin_activity.py 2 23219097987  # конкретный activity_id
"""
import sys
import os
import json
import re
import asyncio
import statistics

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import garmin

# Маска имени нашей тренировки: DD_YYYYMMDD-<группа>_lvl (группа может быть "3.5")
_DD_RE = re.compile(r"DD_(\d{8})-([\d.]+)_lvl")


def _parse_dd_name(name: str):
    """Из имени активности достаёт (workout_date 'YYYY-MM-DD', group). None если нет маски."""
    m = _DD_RE.search(name or "")
    if not m:
        return None, None
    raw_date, group = m.group(1), m.group(2)
    wdate = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
    return wdate, group


def _pace_s(dist_m, dur_s):
    """Темп в секундах на км. None если нет данных (без подстановок)."""
    if not dist_m or not dur_s:
        return None
    return round(dur_s / (dist_m / 1000), 1)


def _fmt_pace(sec_per_km):
    if sec_per_km is None:
        return "—"
    return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}"


def _role_of(intensity_type):
    """ACTIVE→work, RECOVERY/REST→rest, иначе unknown (без угадывания)."""
    it = str(intensity_type or "").upper()
    if it == "ACTIVE":
        return "work"
    if it in ("RECOVERY", "REST", "INTERVAL"):  # INTERVAL у Garmin = отдых между
        return "rest" if it != "INTERVAL" else "rest"
    return "unknown"


def _normalize(act: dict, splits: dict, zones: list) -> dict:
    name = act.get("activityName") or ""
    wdate, group = _parse_dd_name(name)

    laps_raw = []
    if isinstance(splits, dict):
        laps_raw = splits.get("lapDTOs") or splits.get("laps") or []
    laps_raw = [l for l in laps_raw if isinstance(l, dict)]

    laps = []
    for i, lp in enumerate(laps_raw, 1):
        dist = lp.get("distance")
        dur = lp.get("duration") or lp.get("movingDuration")
        avg_spd = lp.get("averageSpeed")
        max_spd = lp.get("maxSpeed")
        # ровность: средняя/макс скорость (1.0 = идеально ровно). None если нет данных
        evenness = round(avg_spd / max_spd, 3) if (avg_spd and max_spd) else None
        laps.append({
            "idx": i,
            "role": _role_of(lp.get("intensityType")),
            "distance_m": round(dist, 1) if dist else None,
            "duration_s": round(dur, 1) if dur else None,
            "avg_pace_s": _pace_s(dist, dur),
            "avg_hr": lp.get("averageHR"),
            "max_hr": lp.get("maxHR"),
            "wkt_step_index": lp.get("wktStepIndex"),
            "compliance_score": lp.get("directWorkoutComplianceScore"),
            "pace_evenness": evenness,
        })

    # tail: аномально короткий последний work (<50% медианной рабочей дистанции)
    work_dists = [l["distance_m"] for l in laps
                  if l["role"] == "work" and l["distance_m"]]
    if work_dists and len(work_dists) >= 3:
        med = statistics.median(work_dists)
        last = laps[-1]
        if (last["role"] == "work" and last["distance_m"]
                and last["distance_m"] < 0.5 * med):
            last["role"] = "tail"

    hr_zones = []
    for z in (zones or []):
        if isinstance(z, dict):
            hr_zones.append({
                "zone": z.get("zoneNumber"),
                "secs": round(z.get("secsInZone") or 0, 1),
                "low_boundary": z.get("zoneLowBoundary"),
            })

    return {
        "source": "garmin",
        "activity_id": act.get("activityId"),
        "date": act.get("startTimeLocal"),
        "name": name,
        "workout_date": wdate,
        "group": group,
        "summary": {
            "distance_m": act.get("distance"),
            "duration_s": act.get("duration"),
            "avg_hr": act.get("averageHR"),
            "max_hr": act.get("maxHR"),
            "training_load": act.get("activityTrainingLoad"),
        },
        "hr_zones": hr_zones,
        "laps": laps,
    }


async def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    forced_id = int(sys.argv[2]) if len(sys.argv) > 2 else None

    client = await garmin._client(uid)
    if not client:
        print(f"user={uid}: нет клиента Garmin")
        return

    if forced_id:
        act = await asyncio.to_thread(client.get_activity, forced_id)
        act_id = forced_id
    else:
        acts = await asyncio.to_thread(client.get_activities, 0, 20)
        runs = [a for a in (acts or [])
                if "running" in str((a.get("activityType") or {}).get("typeKey", ""))
                and "DD_" in str(a.get("activityName") or "")]
        if not runs:
            print("Беговой активности с маской DD_ в последних 20 нет")
            return
        act = runs[0]
        act_id = act.get("activityId")

    splits = await asyncio.to_thread(client.get_activity_splits, act_id)
    try:
        zones = await asyncio.to_thread(client.get_activity_hr_in_timezones, act_id)
    except Exception as e:
        print(f"HR-зоны недоступны: {type(e).__name__}: {e}")
        zones = []

    s4 = _normalize(act, splits, zones)

    # Читаемая сводка
    print(f"=== s4_activity: {s4['name']} ===")
    print(f"  activity_id={s4['activity_id']}  date={s4['date']}")
    print(f"  workout_date={s4['workout_date']}  group={s4['group']}")
    sm = s4["summary"]
    print(f"  дистанция={sm['distance_m']}м  время={sm['duration_s']}с  "
          f"HR {sm['avg_hr']}/{sm['max_hr']}  load={sm['training_load']}")
    print(f"  HR-зоны: " + ", ".join(
        f"Z{z['zone']}={z['secs']:.0f}с" for z in s4["hr_zones"]))

    print(f"\n  Лэпы ({len(s4['laps'])}):")
    for l in s4["laps"]:
        print(f"  {l['idx']:>2}. {l['role']:<8} {l['distance_m'] or 0:>6.0f}м "
              f"{_fmt_pace(l['avg_pace_s']):>5}  HR {l['avg_hr']}/{l['max_hr']}  "
              f"ровность={l['pace_evenness']}  step={l['wkt_step_index']} "
              f"compl={l['compliance_score']}")

    # JSON слоя
    print("\n=== JSON s4_activity ===")
    print(json.dumps(s4, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
