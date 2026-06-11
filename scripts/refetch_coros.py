"""Форс-перезабор сырья COROS + нормализация — для отладки.

Что делает: coros.fetch_raw(uid) — живой забор (dashboard, account, activity,
day_detail с ati/cti), запись в raw_service_data; затем run_normalization
пересобирает unified_cache. Показывает итоговый TR/TSB.

Пишет в базу: raw_service_data (coros), unified_cache. morning_* НЕ трогает.
Импортирует: coros, database, data_normalizer. НЕ импортирует bot.py.

Запуск:
    venv/bin/python3 scripts/refetch_coros.py 6        # Ксения
    venv/bin/python3 scripts/refetch_coros.py 17
"""
import sys
import os
import json
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import coros
import database as db
from data_normalizer import run_normalization


def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 6

    print(f"== fetch_raw(coros) user={uid} ==")
    raw = asyncio.run(coros.fetch_raw(uid))
    if not raw:
        print("fetch_raw вернул None")
        return

    dd = raw.get("day_detail")
    if isinstance(dd, dict):
        items = ((dd.get("data") or {}).get("dataList")) or []
        items = [i for i in items if isinstance(i, dict)]
        print(f"  day_detail: result={dd.get('result')} записей={len(items)}")
        for i in items[-3:]:
            day = i.get("happenDay") or i.get("day") or i.get("date")
            print(f"    day={day} ati={i.get('ati')} cti={i.get('cti')} "
                  f"tired={i.get('tiredRate') or i.get('tired_rate')}")
    else:
        print(f"  day_detail: {dd!r}")

    print("== run_normalization ==")
    run_normalization(uid)
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT unified_json FROM unified_cache WHERE user_id=?", (uid,)
        ).fetchone()
    if row and row[0]:
        u = json.loads(row[0])
        tr = u.get("s3_training_readiness") or {}
        lc = u.get("s3_load_chronic") or {}
        print(f"  TR={tr.get('score')} ({tr.get('level')}) "
              f"rec_daily={u.get('s3_recovery_daily')} "
              f"coros ctl={lc.get('ctl')} atl={lc.get('atl')} tsb={lc.get('tsb')}")


if __name__ == "__main__":
    main()
