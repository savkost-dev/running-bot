"""Шаг 1б ревизии Strava: проверка всех strava-токенов реальным refresh (без записи).

Классификация:
  OK        — refresh прошёл, атлет подключён
  REVOKED   — Strava ответила invalid grant (пользователь отозвал доступ) → кандидат на чистку
  ERROR     — сеть/прочее (не трогать, повторить позже)
Запуск (на сервере): venv/bin/python3 scripts/probe_strava_audit.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import strava  # noqa: E402
from database import get_connection  # noqa: E402


async def main():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM user_tokens WHERE service = 'strava'"
        ).fetchall()
    print(f"strava-токенов: {len(rows)}")
    ok = revoked = err = 0
    for (uid,) in rows:
        from database import get_token
        td = get_token(uid, "strava")
        if not td or not td.get("refresh_token"):
            print(f"  uid={uid}: нет refresh_token → REVOKED-кандидат")
            revoked += 1
            continue
        try:
            resp = await strava.refresh_access_token(td["refresh_token"])
        except Exception as e:
            print(f"  uid={uid}: ERROR ({e})")
            err += 1
            continue
        if "access_token" in resp:
            print(f"  uid={uid}: OK")
            ok += 1
        else:
            print(f"  uid={uid}: REVOKED ({str(resp)[:80]})")
            revoked += 1
    print(f"\nитого: OK={ok}, REVOKED={revoked}, ERROR={err}")


asyncio.run(main())
