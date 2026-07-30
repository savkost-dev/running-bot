"""Управление webhook-подпиской Strava (у приложения может быть ровно одна).

Что происходит при create: мы говорим Strava «шли события на наш адрес».
Strava ТУТ ЖЕ делает GET на callback_url с challenge — бот должен быть
запущен с новым oauth_server (эхо challenge), иначе подписка не создастся.

Запуск (на сервере):
  venv/bin/python3 scripts/manage_strava_webhook.py list
  venv/bin/python3 scripts/manage_strava_webhook.py create
  venv/bin/python3 scripts/manage_strava_webhook.py delete <id>
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import aiohttp  # noqa: E402
import strava  # noqa: E402

SUB_URL = "https://www.strava.com/api/v3/push_subscriptions"
CALLBACK = "http://167.172.185.88:8080/strava/webhook"
VERIFY = os.getenv("STRAVA_WEBHOOK_VERIFY_TOKEN", "dodick-strava-hook")
CREDS = {"client_id": strava.STRAVA_CLIENT_ID,
         "client_secret": strava.STRAVA_CLIENT_SECRET}


async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    async with aiohttp.ClientSession() as s:
        if cmd == "list":
            async with s.get(SUB_URL, params=CREDS) as r:
                print(r.status, await r.text())
        elif cmd == "create":
            async with s.post(SUB_URL, data={
                **CREDS, "callback_url": CALLBACK, "verify_token": VERIFY,
            }) as r:
                print(r.status, await r.text())
        elif cmd == "delete" and len(sys.argv) > 2:
            async with s.delete(f"{SUB_URL}/{sys.argv[2]}", params=CREDS) as r:
                print(r.status, await r.text())
        else:
            print("команды: list | create | delete <id>")


asyncio.run(main())
