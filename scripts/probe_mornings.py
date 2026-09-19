"""Разведка: таблица mornings — колонки и последние записи пользователя.
Запуск: python scripts/probe_mornings.py <путь к базе> [user_id=2]. Только чтение."""
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
uid = int(sys.argv[2]) if len(sys.argv) > 2 else 2
cols = [c[1] for c in con.execute("PRAGMA table_info(mornings)")]
print("колонки:", cols)
for r in con.execute("SELECT * FROM mornings WHERE user_id = ? ORDER BY date DESC LIMIT 5", (uid,)):
    print(dict(zip(cols, r)))
