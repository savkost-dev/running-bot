"""Отметки алерта протухших кредов: кому и когда бот отправил уведомление.

Печатает ключи stale_notice_* из bot_settings. Если после вечерней рассылки
ключ stale_notice_6_coros существует с датой рассылки — сообщение Ксюше ушло.

Запуск (на сервере): venv/bin/python3 scripts/probe_stale_notices.py
"""
import os
import sqlite3

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
conn = sqlite3.connect("running_bot.db")
rows = conn.execute(
    "SELECT key, value FROM bot_settings WHERE key LIKE 'stale_notice_%' ORDER BY key"
).fetchall()
print(f"отметок: {len(rows)}")
for k, v in rows:
    print(f"  {k} = {v}")
conn.close()
