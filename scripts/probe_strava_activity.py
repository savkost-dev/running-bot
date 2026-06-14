"""Пробник Strava: конкретная DD-активность по дате + detail + streams — read-only.

Что делает: по db_user_id берёт живой токен Strava (ensure_valid_token, при
необходимости рефреш), ищет в последних активностях беговую с заданной датой
(start_date_local) и маской 'DD' в названии, затем печатает:
  - сводку активности (какие скалярные поля пришли и не пустые),
  - splits_metric (авто-сплиты по км): сколько, ключи, таблица,
  - laps (лэпы как записаны): сколько, ключи, таблица,
  - best_efforts: список,
  - streams (посекундные ряды): какие ключи доступны, длина и диапазон каждого.
Цель: понять, сколько данных реально отдаёт Strava под шаблон графиков/аналитики.

Пишет в базу: НЕТ (read-only; может обновить токен Strava при рефреше — штатно).
Пишет ФАЙЛ: strava_dd_<date>_uid<uid>.json (сырой detail+streams для разбора).
Импортирует: strava, database. НЕ импортирует bot.py.

Запуск:
    venv/bin/python3 scripts/probe_strava_activity.py                 # uid=2, дата 2026-06-12
    venv/bin/python3 scripts/probe_strava_activity.py 2 2026-06-12    # явно
    venv/bin/python3 scripts/probe_strava_activity.py 6 2026-06-12    # другой uid
"""
import sys
import os
import json
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import aiohttp
from strava import ensure_valid_token, get_recent_activities, get_activity_detail, STRAVA_API_BASE

STREAM_KEYS = [
    "time", "distance", "heartrate", "velocity_smooth", "altitude",
    "cadence", "watts", "grade_smooth", "temp", "moving", "latlng",
]


def _pace_from_vel(v_ms):
    """м/с → 'м:сс'/км. None если нет/ноль."""
    if not v_ms or v_ms <= 0:
        return "—"
    sec_per_km = 1000.0 / v_ms
    return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}"


def _num_range(vals):
    """Диапазон числового ряда (min..max) без None."""
    nums = [x for x in vals if isinstance(x, (int, float))]
    if not nums:
        return "нет чисел"
    return f"min={min(nums):.1f}  max={max(nums):.1f}"


async def _fetch_streams(token, act_id):
    """GET /activities/{id}/streams?keys=...&key_by_type=true → dict|None."""
    headers = {"Authorization": f"Bearer {token}"}
    params = {"keys": ",".join(STREAM_KEYS), "key_by_type": "true"}
    url = f"{STRAVA_API_BASE}/activities/{act_id}/streams"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers, params=params) as r:
            if r.status != 200:
                print(f"  streams: HTTP {r.status}")
                return None
            return await r.json()


async def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    target_date = sys.argv[2] if len(sys.argv) > 2 else "2026-06-12"

    token = await ensure_valid_token(uid)
    if not token:
        print(f"user={uid}: нет токена Strava")
        return

    acts = await get_recent_activities(token, days=14)
    acts = acts or []
    print(f"Активностей за 14 дней: {len(acts)}")
    runs = [a for a in acts if a.get("type") == "Run"]
    print("Беговые (дата / имя):")
    for a in runs[:15]:
        print(f"  {a.get('start_date_local', '')[:10]}  {a.get('name')!r}")

    # Отбор без фолбэков: точная дата + 'DD' в названии.
    match = [a for a in runs
             if a.get("start_date_local", "")[:10] == target_date
             and "DD" in str(a.get("name") or "")]
    if not match:
        print(f"\nБеговой DD-активности на {target_date} в последних 14 днях нет.")
        return
    act = match[0]
    act_id = act.get("id")
    print(f"\n=== Активность {act_id}: {act.get('name')!r}  {act.get('start_date_local')} ===")

    # 1) Detail: какие поля пришли и не пустые
    detail = await get_activity_detail(token, act_id)
    if not detail:
        print("get_activity_detail: пусто/ошибка")
        return

    scalar_fields = [
        "distance", "moving_time", "elapsed_time", "total_elevation_gain",
        "average_speed", "max_speed", "average_heartrate", "max_heartrate",
        "average_cadence", "average_watts", "max_watts", "weighted_average_watts",
        "kilojoules", "calories", "suffer_score", "average_temp",
        "has_heartrate", "device_name", "elev_high", "elev_low", "workout_type",
    ]
    print("\n[СВОДКА — скалярные поля]")
    for f in scalar_fields:
        v = detail.get(f)
        mark = "—" if v is None else v
        print(f"  {f:<24} = {mark}")

    # 2) splits_metric (авто по км)
    sm = detail.get("splits_metric") or []
    print(f"\n[SPLITS_METRIC — авто по км]: {len(sm)}")
    if sm:
        print(f"  ключи сплита: {sorted(sm[0].keys())}")
        for i, s in enumerate(sm, 1):
            d = s.get("distance")
            t = s.get("moving_time") or s.get("elapsed_time")
            print(f"  {i:>2}. {d or 0:>6.0f}м  {t or 0:>5}с  "
                  f"темп {_pace_from_vel(s.get('average_speed')):>5}  "
                  f"HR {s.get('average_heartrate')}  zone {s.get('pace_zone')}  "
                  f"набор {s.get('elevation_difference')}")

    # 3) laps (как записаны)
    laps = detail.get("laps") or []
    print(f"\n[LAPS — как записаны]: {len(laps)}")
    if laps:
        print(f"  ключи лэпа: {sorted(laps[0].keys())}")
        for i, lp in enumerate(laps, 1):
            d = lp.get("distance")
            t = lp.get("moving_time") or lp.get("elapsed_time")
            print(f"  {i:>2}. {d or 0:>7.0f}м  {t or 0:>5}с  "
                  f"темп {_pace_from_vel(lp.get('average_speed')):>5}  "
                  f"HR {lp.get('average_heartrate')}/{lp.get('max_heartrate')}  "
                  f"cad {lp.get('average_cadence')}")

    # 4) best_efforts
    be = detail.get("best_efforts") or []
    print(f"\n[BEST_EFFORTS]: {len(be)}")
    for e in be:
        print(f"  {e.get('name'):<16} {e.get('elapsed_time')}с  дист {e.get('distance')}м")

    # 5) streams — посекундные ряды (основа графиков)
    print("\n[STREAMS — посекундные ряды]")
    streams = await _fetch_streams(token, act_id)
    if isinstance(streams, dict) and streams:
        for key, obj in streams.items():
            data = obj.get("data") if isinstance(obj, dict) else None
            n = len(data) if isinstance(data, list) else 0
            extra = ""
            if data and key in ("heartrate", "altitude", "cadence", "watts", "grade_smooth", "temp", "distance"):
                extra = "  " + _num_range(data)
            elif data and key == "velocity_smooth":
                vmax = max((x for x in data if isinstance(x, (int, float))), default=0)
                extra = f"  макс.темп {_pace_from_vel(vmax)}"
            print(f"  {key:<16} n={n}{extra}")
    else:
        print("  нет streams (или приватная/без сенсоров)")

    # Дамп сырья для разбора шаблона
    out = {"activity_summary": act, "detail": detail, "streams": streams}
    fname = f"strava_dd_{target_date}_uid{uid}.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, default=str, indent=2)
    print(f"\nСырьё сохранено: {fname}")


if __name__ == "__main__":
    asyncio.run(main())
