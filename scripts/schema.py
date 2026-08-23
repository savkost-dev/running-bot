"""\u0421\u0445\u0435\u043c\u0430 \u043b\u043e\u043a\u0430\u043b\u044c\u043d\u043e\u0439 \u043a\u043e\u043f\u0438\u0438 \u0431\u0430\u0437\u044b (20.08.2026, \u0442\u043e\u043b\u044c\u043a\u043e \u0447\u0442\u0435\u043d\u0438\u0435).

\u0417\u0430\u043f\u0443\u0441\u043a \u0438\u0437 D:\\running-bot:  python scripts\\schema.py
\u0411\u0435\u0437 \u0430\u0440\u0433\u0443\u043c\u0435\u043d\u0442\u043e\u0432 \u2014 \u0441\u043f\u0438\u0441\u043e\u043a \u0442\u0430\u0431\u043b\u0438\u0446 \u0438 CREATE \u0434\u043b\u044f users.
\u0421 \u0430\u0440\u0433\u0443\u043c\u0435\u043d\u0442\u043e\u043c \u2014 CREATE \u0443\u043a\u0430\u0437\u0430\u043d\u043d\u043e\u0439 \u0442\u0430\u0431\u043b\u0438\u0446\u044b:  python scripts\\schema.py user_profile
"""
import os
import sqlite3
import sys

DB = os.path.join(os.path.dirname(__file__), "..", "data", "running_bot.db")


def main():
    conn = sqlite3.connect(DB)
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("\u0442\u0430\u0431\u043b\u0438\u0446\u044b:", ", ".join(names), "\n")

    want = sys.argv[1] if len(sys.argv) > 1 else "users"
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (want,)).fetchone()
    print(row[0] if row else f"\u0442\u0430\u0431\u043b\u0438\u0446\u0430 {want} \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430")

    if want == "users":
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        print("\n\u043a\u043e\u043b\u043e\u043d\u043a\u0438 users:", ", ".join(cols))
        print("\u0432\u0441\u0435\u0433\u043e \u0437\u0430\u043f\u0438\u0441\u0435\u0439:",
              conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


if __name__ == "__main__":
    main()
