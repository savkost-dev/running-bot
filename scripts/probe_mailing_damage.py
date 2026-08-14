"""Урон от обрыва рассылки: кто остался без рекомендации на дату.

Сравнивает активных подписанных (notify_interval) с теми, у кого есть
last_recommendation на дату тренировки. Показывает получивших/оставшихся.

Запуск (на сервере): venv/bin/python3 scripts/probe_mailing_damage.py 2026-08-14
"""
import os
import sqlite3
import sys

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
wdate = sys.argv[1] if len(sys.argv) > 1 else "2026-08-14"
conn = sqlite3.connect("running_bot.db")

subscribed = {r[0]: (r[1] or r[2] or f"user_{r[0]}") for r in conn.execute("""
    SELECT u.id, u.username, u.name FROM users u
    LEFT JOIN user_preferences p ON u.id = p.user_id
    WHERE (p.is_active IS NULL OR p.is_active = 1)
      AND (p.notify_interval IS NULL OR p.notify_interval = 1)""")}
got = {r[0] for r in conn.execute(
    "SELECT user_id FROM last_recommendation WHERE workout_date = ?", (wdate,))}

print(f"дата: {wdate}")
print(f"подписанных активных: {len(subscribed)}")
print(f"рекомендация построена: {len(got & set(subscribed))}")
missing = sorted(set(subscribed) - got)
print(f"без рекомендации: {len(missing)}")
for uid in missing:
    print(f"  uid={uid:3d} @{subscribed[uid]}")
conn.close()
