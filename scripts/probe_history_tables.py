"""Разведка: таблицы, похожие на историю восстановления/утра — число строк и колонки с датой.
Запуск: python scripts/probe_history_tables.py <путь к базе>. Только чтение."""
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    if any(k in t for k in ("recover", "morning", "raw", "snapshot", "cache", "history", "sleep", "hrv")):
        cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(t, n, [c for c in cols if "date" in c or c.endswith("_at")])
