"""COROS: почему у части пользователей не ловится утро — проверка гипотезы регионов.

Для всех активных с COROS показывает: coros_region из профиля, свежесть
raw_service_data('coros'), размер сырья и есть ли в нём сон.
Сравниваем работающих (снимки идут) с неработающими — если у вторых регион
пуст/другой или сырьё без сна, гипотеза подтверждена.

Запуск (на сервере): venv/bin/python3 scripts/probe_coros_region.py
"""
import os
import sqlite3
import sys

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
conn = sqlite3.connect("running_bot.db")

rows = conn.execute("""
    SELECT u.id, COALESCE(u.username, u.name),
           p.coros_region,
           CASE WHEN p.coros_email IS NOT NULL THEN 1 ELSE 0 END,
           r.fetched_at, LENGTH(r.raw_json),
           CASE WHEN r.raw_json LIKE '%sleep%' THEN 1 ELSE 0 END,
           (SELECT MAX(date) FROM mornings m
             WHERE m.user_id = u.id AND m.morning_caught = 1)
    FROM users u
    JOIN user_tokens t ON t.user_id = u.id AND t.service = 'coros'
    LEFT JOIN user_preferences pref ON pref.user_id = u.id
    LEFT JOIN user_profile p ON p.user_id = u.id
    LEFT JOIN raw_service_data r ON r.user_id = u.id AND r.service = 'coros'
    WHERE pref.is_active IS NULL OR pref.is_active = 1
    ORDER BY u.id
""").fetchall()

print(f"{'uid':>4} {'кто':22} {'регион':8} {'creds':5} "
      f"{'raw fetched_at':20} {'байт':>7} {'сон':>3} {'посл.утро':10}")
for uid, name, region, creds, fat, rlen, has_sleep, last_m in rows:
    print(f"{uid:>4} {(name or '—'):22} {(region or '—'):8} {creds:>5} "
          f"{(fat or '—'):20} {(rlen or 0):>7} {('да' if has_sleep else '—'):>3} "
          f"{(last_m or 'никогда'):10}")
conn.close()
