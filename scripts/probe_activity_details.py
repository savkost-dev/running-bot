"""Пробник Garmin get_activity_details — посекундные ряды последней DD-активности (read-only).

Зачем: понять, можно ли считать НАСТОЯЩУЮ ровность темпа ВНУТРИ отрезка (форма бега
по ходу 200/800 м), а не грубый прокси avg/max из лэпа. Для этого нужен ряд точек
внутри лэпа — этот скрипт смотрит, что отдаёт get_activity_details:
  - top-level ключи ответа;
  - metricDescriptors: индекс → metricsKey (единица) — какие метрики на точку
    (есть ли directSpeed / sumDistance / directTimestamp / directHeartRate);
  - сколько точек всего и шаг дискретизации (медиана dt по таймстемпам);
  - 3 примера декодированных точек (время/дистанция/скорость→темп/HR);
  - границы лэпов из splits (startTimeGMT + накопленная дистанция) — чтобы оценить,
    как привязывать точки к конкретному отрезку.

Ничего не строит и НЕ пишет в БД. Может обновить токен Garmin при реавторизации — штатно.
Импортирует: garmin (без bot.py). НЕ импортирует bot.py.

Запуск (uid по умолчанию 2 = Anton):
    venv/bin/python3 scripts/probe_activity_details.py                 # последняя DD
    venv/bin/python3 scripts/probe_activity_details.py DD_20260612     # по маске
    venv/bin/python3 scripts/probe_activity_details.py 23219097987     # по activityId
"""
import sys
import os
import asyncio
import statistics

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import garmin


def _pace(speed_ms):
    if not speed_ms or speed_ms <= 0:
        return "—"
    s = 1000.0 / speed_ms
    return f"{int(s // 60)}:{int(s % 60):02d}"


def _resolve(acts, selector):
    runs = [a for a in (acts or [])
            if "running" in str((a.get("activityType") or {}).get("typeKey", ""))
            and "DD_" in str(a.get("activityName") or "")]
    if selector is None:
        return runs[0] if runs else None
    if selector.isdigit():
        return next((a for a in (acts or []) if str(a.get("activityId")) == selector), None)
    return next((a for a in runs if selector in str(a.get("activityName") or "")), None)


async def main():
    selector = sys.argv[1] if len(sys.argv) > 1 else None
    uid = 2

    client = await garmin._client(uid)
    if not client:
        print(f"user={uid}: нет клиента Garmin")
        return

    acts = await asyncio.to_thread(client.get_activities, 0, 20)
    act = _resolve(acts, selector)
    if not act:
        print(f"По селектору {selector!r} активность не найдена.")
        return
    act_id = act.get("activityId")
    print(f"=== {act.get('activityName')!r}  id={act_id} ===")

    # детали (просим высокое разрешение, чтобы не даунсэмплить)
    details = None
    for args in ((act_id, 100000, 100000), (act_id,)):
        try:
            details = await asyncio.to_thread(client.get_activity_details, *args)
            break
        except Exception as e:
            print(f"get_activity_details{args[1:]}: {type(e).__name__}: {e}")
    if not isinstance(details, dict):
        print(f"details не dict: {type(details)}")
        return

    print(f"\ntop-level keys: {sorted(details.keys())}")
    for k in ("totalMetricsCount", "metricsCount", "measurementCount"):
        if k in details:
            print(f"  {k} = {details[k]}")

    descs = details.get("metricDescriptors") or []
    print(f"\nmetricDescriptors ({len(descs)}):")
    key_to_idx = {}
    for d in sorted(descs, key=lambda x: x.get("metricsIndex", 0)):
        key = d.get("key") or d.get("metricsKey")
        idx = d.get("metricsIndex")
        unit = (d.get("unit") or {}).get("key") if isinstance(d.get("unit"), dict) else d.get("unit")
        key_to_idx[key] = idx
        print(f"  [{idx:>2}] {key}  ({unit})")

    rows = details.get("activityDetailMetrics") or []
    print(f"\nточек (activityDetailMetrics): {len(rows)}")
    if not rows:
        print("  рядов нет — детальные ряды недоступны для этой активности.")
        return

    def val(row, key):
        i = key_to_idx.get(key)
        m = row.get("metrics") if isinstance(row, dict) else None
        if i is None or not m or i >= len(m):
            return None
        return m[i]

    # шаг дискретизации по времени
    ts_key = next((k for k in key_to_idx if "timestamp" in (k or "").lower()), None)
    if ts_key:
        ts = [val(r, ts_key) for r in rows]
        ts = [t for t in ts if t is not None]
        if len(ts) >= 2:
            diffs = [ (ts[i+1]-ts[i]) for i in range(len(ts)-1) if ts[i+1] and ts[i] ]
            if diffs:
                md = statistics.median(diffs)
                print(f"  таймстемп-ключ: {ts_key}; медианный шаг ≈ {md} (ед. ключа), "
                      f"span ≈ {round((ts[-1]-ts[0]))}")

    # интересные ключи
    spd_key = next((k for k in key_to_idx if k and "Speed" in k), None)
    dist_key = next((k for k in key_to_idx if k and ("sumDistance" in k or k == "distance")), None)
    hr_key = next((k for k in key_to_idx if k and "HeartRate" in k), None)
    print(f"\n  ключи: speed={spd_key}  distance={dist_key}  hr={hr_key}")

    print("\n  3 примера точек:")
    for r in rows[:3]:
        spd = val(r, spd_key) if spd_key else None
        print(f"    t={val(r, ts_key) if ts_key else '—'}  "
              f"dist={val(r, dist_key) if dist_key else '—'}  "
              f"speed={spd} → темп {_pace(spd)}  hr={val(r, hr_key) if hr_key else '—'}")

    # границы лэпов из splits — для оценки привязки точек к отрезку
    try:
        splits = await asyncio.to_thread(client.get_activity_splits, act_id)
        laps = (splits.get("lapDTOs") or []) if isinstance(splits, dict) else []
        print(f"\nЛэпы для привязки ({len(laps)}): startTimeGMT + накопл. дистанция")
        cum = 0.0
        for i, lp in enumerate(laps[:6], 1):
            cum += lp.get("distance") or 0
            print(f"  {i:>2}. {lp.get('intensityType'):<10} start={lp.get('startTimeGMT')}  "
                  f"dist_лэпа={lp.get('distance')}  накопл≈{cum:.0f}")
        if len(laps) > 6:
            print(f"  … ещё {len(laps)-6}")
    except Exception as e:
        print(f"splits для привязки недоступны: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
