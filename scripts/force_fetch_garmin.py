"""Пересбор утреннего слепка с нуля (Garmin): сырьё → нормализация → снимок.

Что делает:
  1) garmin.fetch_raw(uid) — живой забор сырья (тот же код, что в боте);
  2) run_normalization(uid) — обновляет unified_cache.unified_json;
  3) собирает снимок (TR/BB/HRV/RHR/сон/подъём) из свежего сырья —
     логика повторяет bot._collect_morning_snapshot (Garmin + Whoop-фоллбек);
  4) показывает снимок; пишет в morning_* ТОЛЬКО с флагом apply.

Пишет в базу: raw_service_data, unified_cache (всегда);
              morning_* (только с apply).
Импортирует: garmin, whoop, database, data_normalizer. НЕ импортирует bot.py.

Запуск:
    venv/bin/python3 scripts/force_fetch_garmin.py            # uid=2, показ без записи снимка
    venv/bin/python3 scripts/force_fetch_garmin.py 2 apply    # + запись morning_*
"""
import sys
import os
import json
import asyncio
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import garmin
import database as db
from data_normalizer import run_normalization

MSK = timezone(timedelta(hours=3))


def collect_snapshot(uid: int) -> dict:
    """Снимок из сырья: копия логики bot._collect_morning_snapshot (Garmin + Whoop)."""
    def _raw(svc):
        row = db.get_raw_service_data(uid, svc)
        if not row:
            return None
        try:
            return json.loads(row["raw_json"])
        except Exception:
            return None

    snap = {"tr": None, "bb": None, "hrv": None, "rhr": None,
            "sleep_h": None, "wake_at": None,
            "snapshot_at": datetime.now(timezone.utc).isoformat()}

    # ── Garmin ──
    if db.get_token(uid, "garmin"):
        g = _raw("garmin") or {}
        us = g.get("user_summary") or {}
        if us.get("bodyBatteryAtWakeTime") is not None:
            snap["bb"] = int(us["bodyBatteryAtWakeTime"])
        if us.get("restingHeartRate"):
            snap["rhr"] = int(us["restingHeartRate"])
        dto = (g.get("sleep_data") or {}).get("dailySleepDTO") or {}
        if dto.get("sleepTimeSeconds"):
            snap["sleep_h"] = round(int(dto["sleepTimeSeconds"]) / 3600, 2)
        elif us.get("sleepingSeconds"):
            snap["sleep_h"] = round(int(us["sleepingSeconds"]) / 3600, 2)
        wake_ms = dto.get("sleepEndTimestampLocal")
        wake_local = None
        if wake_ms:
            wake_local = datetime.utcfromtimestamp(int(wake_ms) / 1000)
            snap["wake_at"] = wake_local.isoformat()
        elif us.get("wellnessEndTimeLocal"):
            snap["wake_at"] = str(us["wellnessEndTimeLocal"])
        # TR: первая запись ПОСЛЕ пробуждения; если таких нет — самая ранняя
        tr_raw = g.get("training_readiness")
        tr_list = tr_raw if isinstance(tr_raw, list) else ([tr_raw] if tr_raw else [])
        tr_cands = [t for t in tr_list
                    if isinstance(t, dict) and t.get("score") is not None
                    and t.get("timestampLocal")]
        if tr_cands:
            def _tr_local(t):
                try:
                    return datetime.fromisoformat(t["timestampLocal"])
                except Exception:
                    return datetime.max
            after_wake = ([t for t in tr_cands if _tr_local(t) >= wake_local]
                          if wake_local else [])
            pick = (min(after_wake, key=_tr_local) if after_wake
                    else min(tr_cands, key=_tr_local))
            snap["tr"] = int(pick["score"])
        hrv_sum = (g.get("hrv_data") or {}).get("hrvSummary") or {}
        if hrv_sum.get("lastNightAvg") is not None:
            snap["hrv"] = float(hrv_sum["lastNightAvg"])

    # ── Whoop (фоллбек для незаполненных) ──
    if db.get_token(uid, "whoop"):
        w = _raw("whoop") or {}
        rec = (w.get("recovery") or {}).get("records") or []
        if rec:
            sc = rec[0].get("score") or {}
            if snap["hrv"] is None and sc.get("hrv_rmssd_milli") is not None:
                snap["hrv"] = round(float(sc["hrv_rmssd_milli"]), 1)
            if snap["rhr"] is None and sc.get("resting_heart_rate") is not None:
                snap["rhr"] = int(round(float(sc["resting_heart_rate"])))
        slp = (w.get("sleep") or {}).get("records") or []
        if slp:
            s0 = slp[0]
            if snap["wake_at"] is None and s0.get("end"):
                snap["wake_at"] = str(s0["end"])
            stage = (s0.get("score") or {}).get("stage_summary") or {}
            total_ms = ((stage.get("total_light_sleep_time_milli") or 0) +
                        (stage.get("total_slow_wave_sleep_time_milli") or 0) +
                        (stage.get("total_rem_sleep_time_milli") or 0))
            if snap["sleep_h"] is None and total_ms:
                snap["sleep_h"] = round(total_ms / 3_600_000, 2)

    return snap


def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    apply = "apply" in sys.argv[2:]

    print(f"== 1. fetch_raw(garmin) user={uid} ==")
    raw = asyncio.run(garmin.fetch_raw(uid))
    if not raw:
        print("fetch_raw вернул None — данных нет, стоп")
        return
    dto = (raw.get("sleep_data") or {}).get("dailySleepDTO") or {}
    us = raw.get("user_summary") or {}
    print(f"  sleepEndLocal={dto.get('sleepEndTimestampLocal')}  "
          f"sleepSecs={dto.get('sleepTimeSeconds')}  "
          f"BBatWake={us.get('bodyBatteryAtWakeTime')}")

    print("== 2. run_normalization ==")
    run_normalization(uid)

    print("== 3. снимок из свежего сырья ==")
    snap = collect_snapshot(uid)
    print(f"  tr={snap['tr']} bb={snap['bb']} hrv={snap['hrv']} rhr={snap['rhr']} "
          f"sleep_h={snap['sleep_h']} wake_at={snap['wake_at']}")

    if apply:
        today_msk = datetime.now(MSK).strftime("%Y-%m-%d")
        db.set_morning_caught(uid, today_msk, snapshot=snap)
        print(f"== 4. записано в morning_* (date={today_msk}) ==")
    else:
        print("== 4. БЕЗ записи morning_* (добавь аргумент apply) ==")


if __name__ == "__main__":
    main()
