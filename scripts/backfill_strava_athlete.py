"""Бэкфилл strava_athlete_id для существующих подключений.

Что делает: по каждому strava-токену из user_tokens берёт валидный access
(ensure_valid_token сам освежит при необходимости), спрашивает Strava
GET /athlete («кто владелец этого токена?») и пишет athlete.id в новую
колонку strava_athlete_id. Это словарь «номер атлета в Strava → наш user_id»,
без которого события вебхука (в них только owner_id) не привязать к людям.

10 пользователей = максимум 20 запросов (refresh + athlete) — лимиты не задевает.
Идемпотентен: уже заполненные пропускает.

Запуск (просмотр): venv/bin/python3 scripts/backfill_strava_athlete.py
Применить:         venv/bin/python3 scripts/backfill_strava_athlete.py --apply
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import aiohttp  # noqa: E402
import strava  # noqa: E402
from database import get_connection, set_strava_athlete_id  # noqa: E402

APPLY = "--apply" in sys.argv


async def main():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id, strava_athlete_id FROM user_tokens WHERE service = 'strava'"
        ).fetchall()
    print(f"strava-подключений: {len(rows)}")
    done = filled = err = 0
    for uid, existing in rows:
        if existing:
            print(f"  uid={uid}: уже заполнено ({existing}) — пропуск")
            done += 1
            continue
        token = await strava.ensure_valid_token(uid)
        if not token:
            print(f"  uid={uid}: нет валидного токена — пропуск")
            err += 1
            continue
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{strava.STRAVA_API_BASE}/athlete",
                                 headers={"Authorization": f"Bearer {token}"}) as resp:
                    data = await resp.json()
        except Exception as e:
            print(f"  uid={uid}: ошибка API ({e})")
            err += 1
            continue
        aid = data.get("id")
        if not aid:
            print(f"  uid={uid}: athlete id не пришёл ({str(data)[:80]})")
            err += 1
            continue
        name = f"{data.get('firstname', '')} {data.get('lastname', '')}".strip()
        print(f"  uid={uid}: athlete_id={aid} ({name})" + ("" if APPLY else " [просмотр]"))
        if APPLY:
            set_strava_athlete_id(uid, aid)
            filled += 1
    print(f"\nитого: заполнено={filled}, было={done}, ошибок={err}")
    if not APPLY:
        print("Просмотр без записи. Для применения добавь --apply")


asyncio.run(main())
