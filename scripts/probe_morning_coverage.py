"""Покрытие утреннего снимка: у скольких пользователей реально ловится утро.

Считает по таблице mornings за последние 7 дней: сколько уникальных активных
пользователей имели morning_caught=1, сколько сегодня, и у кого из активных
снимка не было ни разу (те живут без данных готовности).

Запуск (на сервере): venv/bin/python3 scripts/probe_morning_coverage.py
"""
import os
import sqlite3
import sys

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
conn = sqlite3.connect("running_bot.db")

active = {r[0] for r in conn.execute("""
    SELECT u.id FROM users u
    LEFT JOIN user_preferences p ON u.id = p.user_id
    WHERE p.is_active IS NULL OR p.is_active = 1""")}

rows = conn.execute("""
    SELECT user_id, COUNT(*), MAX(date) FROM mornings
    WHERE morning_caught = 1 AND date >= date('now', '-7 days')
    GROUP BY user_id ORDER BY COUNT(*) DESC""").fetchall()
caught = [(u, n, d) for u, n, d in rows if u in active]

today = conn.execute(
    "SELECT COUNT(DISTINCT user_id) FROM mornings "
    "WHERE morning_caught = 1 AND date = date('now')").fetchone()[0]

names = dict(conn.execute(
    "SELECT id, COALESCE(username, name, 'user_' || id) FROM users"))

print(f"активных: {len(active)}")
print(f"утро ловится (за 7 дней): {len(caught)} · сегодня: {today}\n")
for u, n, d in caught:
    print(f"  uid={u:3d} @{names.get(u, '?'):24s} дней: {n}  последний: {d}")

never = sorted(active - {u for u, _, _ in caught})
with_tracker = {r[0] for r in conn.execute(
    "SELECT DISTINCT user_id FROM user_tokens")} | {r[0] for r in conn.execute(
    "SELECT user_id FROM user_profile WHERE garmin_email IS NOT NULL")}
never_with_tracker = [u for u in never if u in with_tracker]
print(f"\nбез единого снимка за 7 дней: {len(never)} активных, "
      f"из них с трекером: {len(never_with_tracker)}")
for u in never_with_tracker:
    print(f"  uid={u:3d} @{names.get(u, '?')}")
conn.close()
