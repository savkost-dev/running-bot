"""Шаг 1а-2: ищем, где живут токены сервисов (strava и прочие).
Запуск (на сервере): venv/bin/python3 scripts/probe_tokens2.py running_bot.db"""
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "running_bot.db"
conn = sqlite3.connect(DB)
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("все таблицы:", tables)
for t in tables:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
    tok = [c for c in cols if any(k in c.lower() for k in ("token", "auth", "service", "provider"))]
    if tok:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"\n=== {t} ({n} строк) ===\nколонки: {cols}")
        safe = [c for c in cols if "token" not in c.lower() and "secret" not in c.lower()][:8]
        for r in conn.execute(f"SELECT {', '.join(safe)} FROM {t} LIMIT 15"):
            print(" ", dict(zip(safe, r)))
conn.close()
