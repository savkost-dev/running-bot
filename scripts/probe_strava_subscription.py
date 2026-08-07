"""Живость Strava-вебхука и состояние подключений перед пересдачей на квоту.

Проверяет три вещи:
  1. GET /push_subscriptions — существует ли подписка (id, callback_url, даты);
  2. валидность токенов всех подключённых пользователей (ensure_valid_token);
  3. сводку: сколько активных подключений против ёмкости приложения.

Счётчик атлетов в кабинете Strava может отличаться от нашего (он учитывает
все авторизации приложения) — точное число смотреть в settings → My API Application.

Запуск (на сервере): venv/bin/python3 scripts/probe_strava_subscription.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import aiohttp  # noqa: E402
import strava  # noqa: E402
import database as db  # noqa: E402


async def main():
    # 1. Подписка
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{strava.STRAVA_API_BASE}/push_subscriptions",
            params={"client_id": strava.STRAVA_CLIENT_ID,
                    "client_secret": strava.STRAVA_CLIENT_SECRET},
        ) as resp:
            subs = await resp.json()
    print("push_subscriptions:", subs if subs else "ПУСТО — подписки нет!")

    # 2. Токены подключённых
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id, strava_athlete_id FROM user_tokens "
            "WHERE service = 'strava' ORDER BY user_id").fetchall()
    print(f"\nподключений в БД: {len(rows)}")
    ok = bad = 0
    for uid, aid in rows:
        token = await strava.ensure_valid_token(uid)
        status = "OK " if token else "БИТЫЙ"
        if token:
            ok += 1
        else:
            bad += 1
        print(f"  uid={uid:3d} athlete_id={aid or '—':>12} токен: {status}")
    print(f"\nитого: активных {ok}, битых {bad}")
    print("Точный счётчик атлетов/ёмкость — в кабинете Strava: settings → My API Application")


asyncio.run(main())
