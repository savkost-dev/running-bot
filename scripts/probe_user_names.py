"""Кто есть кто: имена пользователей по db_user_id.

Запуск (на сервере): venv/bin/python3 scripts/probe_user_names.py 63 68 75
Без аргументов покажет всех.
"""
import os
import sqlite3
import sys

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
ids = [int(a) for a in sys.argv[1:] if a.isdigit()]
conn = sqlite3.connect("running_bot.db")
cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
name_cols = [c for c in cols if c in (
    "name", "full_name", "username", "first_name", "last_name", "telegram_id", "id")]
q = f"SELECT {', '.join(name_cols)} FROM users"
if ids:
    q += f" WHERE id IN ({', '.join('?' * len(ids))})"
for row in conn.execute(q, ids):
    print(dict(zip(name_cols, row)))
conn.close()
