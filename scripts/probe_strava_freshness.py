"""Свежесть Strava-данных по слоям после перехода на вебхуки (08.08.2026).

Для каждого пользователя со Strava-токеном показывает временные метки трёх слоёв:
  raw_service_data (service=strava)  — слой 1, пишет вебхук-ингест (fetch_raw)
  unified_cache                      — слой 2, run_normalization
  athlete_cache                      — слой 3, refresh_athlete_cache (CTL/ATL/TSB)

Как читать после тренировки: у бегавших сегодня метки всех трёх слоёв должны быть
СЕГОДНЯШНИЕ и близкие ко времени синка активности (не ночные). У не бегавших —
метки старые, и это НОРМА: событий не было, ходить в API незачем.

Схему таблиц не предполагает: сам находит колонки времени (…_at / …time…).

Запуск (на сервере): venv/bin/python3 scripts/probe_strava_freshness.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import database as db  # noqa: E402

TABLES = ("raw_service_data", "unified_cache", "athlete_cache")


def _time_cols(conn, table):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    return cols, [c for c in cols if "_at" in c or "time" in c.lower() or "date" in c.lower()]


with db.get_connection() as conn:
    uids = [r[0] for r in conn.execute(
        "SELECT user_id FROM user_tokens WHERE service='strava' ORDER BY user_id")]
    print(f"Strava-пользователей: {len(uids)}\n")
    for table in TABLES:
        cols, tcols = _time_cols(conn, table)
        has_service = "service" in cols
        has_user = "user_id" in cols
        print(f"── {table} (метки: {', '.join(tcols) or 'не найдены'}) " + "─" * 10)
        for uid in uids:
            q = f"SELECT {', '.join(tcols)} FROM {table} WHERE user_id = ?"
            args = [uid]
            if has_service:
                q += " AND service = 'strava'"
            rows = conn.execute(q, args).fetchall() if (tcols and has_user) else []
            if rows:
                vals = " | ".join(str(v) for v in rows[-1])
                print(f"  uid={uid:3d}: {vals}")
            else:
                print(f"  uid={uid:3d}: нет записи")
        print()
