"""Диагностика «почему ночь не поймана» — read-only.

Находит юзера по подстроке имени/username и печатает ключевые поля
из сырья Garmin (sleepEndLocal, наличие TR/BB, время fetch).
Без аргумента — печатает список всех юзеров (id | name | username).

Запуск на сервере:
    venv/bin/python3 scripts/debug_night.py            # список всех юзеров
    venv/bin/python3 scripts/debug_night.py Pasha       # поиск по подстроке
"""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import database as db


def main():
    users = db.get_users_list_for_b()

    if len(sys.argv) < 2:
        print("Все юзеры (id | name | username):")
        for u in users:
            print(f"  {u['db_user_id']:>3} | {u['name']} | {u['username']}")
        print("\nПодсказка: запусти с подстрокой имени, напр. debug_night.py Pasha")
        return

    needle = sys.argv[1].lower()
    matches = [
        u for u in users
        if needle in (u["name"] or "").lower()
        or needle in (u["username"] or "").lower()
    ]

    if not matches:
        print(f"Никого не нашёл по '{sys.argv[1]}'. Список всех:")
        for u in users:
            print(f"  {u['db_user_id']:>3} | {u['name']} | {u['username']}")
        return

    for u in matches:
        uid = u["db_user_id"]
        print(f"\n=== {u['name']} (@{u['username']}) · id={uid} ===")

        # Какие сервисы подключены
        svcs = [s for s in ("garmin", "coros", "polar", "whoop", "strava")
                if db.get_token(uid, s)]
        print(f"  сервисы: {', '.join(svcs) or '—'}")

        # Утренний снимок
        snap = db.get_morning_caught(uid)
        if snap:
            print(f"  morning_caught: caught={snap['caught']} date={snap['date']} "
                  f"tr={snap['tr']} bb={snap['bb']}")
        else:
            print("  morning_caught: нет записи")

        # Сырьё Garmin
        r = db.get_raw_service_data(uid, "garmin")
        if not r:
            print("  garmin raw: нет")
            continue
        print(f"  garmin fetched_at: {r['fetched_at']}")
        try:
            g = json.loads(r["raw_json"])
        except Exception as e:
            print(f"  garmin raw: ошибка парсинга {e}")
            continue
        dto = (g.get("sleep_data") or {}).get("dailySleepDTO") or {}
        us = g.get("user_summary") or {}
        print(f"  sleepEndLocal: {dto.get('sleepEndTimestampLocal')}")
        print(f"  sleepTimeSeconds: {dto.get('sleepTimeSeconds')}")
        tr_raw = g.get("training_readiness")
        tr_score = None
        if tr_raw:
            item = tr_raw[0] if isinstance(tr_raw, list) else tr_raw
            if isinstance(item, dict):
                tr_score = item.get("score")
        print(f"  TR present: {bool(tr_raw)} (score={tr_score})")
        print(f"  BBatWake: {us.get('bodyBatteryAtWakeTime')}  "
              f"BBrecent: {us.get('bodyBatteryMostRecentValue')}")


if __name__ == "__main__":
    main()
