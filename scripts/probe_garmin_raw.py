"""Пробник утреннего слепка по сырью Garmin — read-only.

Берёт УЖЕ сохранённое сырьё из raw_service_data (ничего не запрашивает у Garmin,
ничего не пишет в БД) и печатает фактические пути/значения всех полей,
которые входят в утренний слепок: TR, BB, сон, HRV, RHR + время синка.

Цель — увидеть, где реально лежат данные в свежем ответе, и почему парсер
(_collect_morning_snapshot / normalize_garmin) достаёт None.

Запуск:
    venv/bin/python3 scripts/probe_garmin_raw.py            # user=2 (Anton) по умолчанию
    venv/bin/python3 scripts/probe_garmin_raw.py 9          # другой db_user_id
"""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import database as db


def _g(d, *path):
    """Безопасно идёт по вложенным ключам/индексам."""
    cur = d
    for p in path:
        if cur is None:
            return None
        if isinstance(p, int):
            if isinstance(cur, list) and -len(cur) <= p < len(cur):
                cur = cur[p]
            else:
                return None
        else:
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                return None
    return cur


def _keys(d):
    if isinstance(d, dict):
        return list(d.keys())
    if isinstance(d, list):
        return f"[list len={len(d)}]"
    return type(d).__name__


def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    row = db.get_raw_service_data(uid, "garmin")
    if not row:
        print(f"user={uid}: нет сырья garmin")
        return
    print(f"user={uid}  fetched_at={row['fetched_at']}  (UTC; +3 = МСК)")
    try:
        g = json.loads(row["raw_json"])
    except Exception as e:
        print(f"ошибка парсинга raw_json: {e}")
        return

    print(f"\nверхние ключи сырья: {_keys(g)}")

    # ── user_summary ──
    us = g.get("user_summary")
    print(f"\n[user_summary] тип/ключи: {_keys(us)}")
    if isinstance(us, dict):
        for k in ("bodyBatteryAtWakeTime", "bodyBatteryMostRecentValue",
                  "bodyBatteryHighestValue", "bodyBatteryLowestValue",
                  "restingHeartRate", "sleepingSeconds",
                  "lastSyncTimestampGMT", "wellnessEndTimeLocal", "wellnessEndTimeGmt"):
            print(f"    {k} = {us.get(k)!r}")

    # ── sleep_data ──
    sd = g.get("sleep_data")
    print(f"\n[sleep_data] тип/ключи: {_keys(sd)}")
    dto = _g(g, "sleep_data", "dailySleepDTO")
    print(f"[sleep_data.dailySleepDTO] тип/ключи: {_keys(dto)}")
    if isinstance(dto, dict):
        for k in ("sleepTimeSeconds", "sleepEndTimestampLocal", "sleepEndTimestampGMT",
                  "sleepStartTimestampLocal", "sleepStartTimestampGMT",
                  "calendarDate", "sleepScores"):
            print(f"    {k} = {dto.get(k)!r}")

    # ── training_readiness ──
    tr = g.get("training_readiness")
    print(f"\n[training_readiness] тип: {_keys(tr)}")
    tr_item = tr[0] if isinstance(tr, list) and tr else (tr if isinstance(tr, dict) else None)
    if isinstance(tr_item, dict):
        for k in ("score", "level", "timestamp", "timestampLocal"):
            print(f"    {k} = {tr_item.get(k)!r}")

    # ── hrv_data ──
    hd = g.get("hrv_data")
    print(f"\n[hrv_data] тип/ключи: {_keys(hd)}")
    summ = _g(g, "hrv_data", "hrvSummary")
    print(f"[hrv_data.hrvSummary] тип/ключи: {_keys(summ)}")
    if isinstance(summ, dict):
        for k in ("lastNightAvg", "weeklyAvg", "status"):
            print(f"    {k} = {summ.get(k)!r}")

    print("\n— конец —")


if __name__ == "__main__":
    main()
