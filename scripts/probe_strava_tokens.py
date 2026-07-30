"""Шаг 1а ревизии Strava: где и как лежат токены (схема + записи, БЕЗ вызовов API).
Запуск (на сервере): venv/bin/python3 scripts/probe_strava_tokens.py running_bot.db"""
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "running_bot.db"
conn = sqlite3.connect(DB)
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
hits = [t for t in tables if "strava" in t.lower()]
print("таблицы со strava:", hits or "нет — ищем колонки")
if not hits:
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        sc = [c for c in cols if "strava" in c.lower()]
        if sc:
            print(f"  {t}: {sc}")
            hits.append((t, sc))
for h in hits:
    t = h if isinstance(h, str) else h[0]
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
    print(f"\n=== {t} ===\nколонки: {cols}")
    safe = [c for c in cols if "token" not in c.lower() and "secret" not in c.lower()]
    rows = conn.execute(f"SELECT {', '.join(safe)} FROM {t}").fetchall()
    for r in rows:
        print(" ", dict(zip(safe, r)))
conn.close()
