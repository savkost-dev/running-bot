"""\u041a\u0442\u043e \u043e\u0442\u043a\u043b\u044e\u0447\u0438\u043b \u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f (20.08.2026, \u0442\u043e\u043b\u044c\u043a\u043e \u0447\u0442\u0435\u043d\u0438\u0435).

\u041f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442 \u0441\u0432\u043e\u0434\u043a\u0443 \u043f\u043e \u0447\u0435\u0442\u044b\u0440\u0451\u043c \u0433\u0430\u043b\u043e\u0447\u043a\u0430\u043c \u044d\u043a\u0440\u0430\u043d\u0430 \u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u0439 \u0438 \u043f\u043e\u0438\u043c\u0451\u043d\u043d\u043e \u0442\u0435\u0445,
\u0443 \u043a\u043e\u0433\u043e \u0445\u043e\u0442\u044f \u0431\u044b \u043e\u0434\u043d\u0430 \u0432\u044b\u043a\u043b\u044e\u0447\u0435\u043d\u0430.

\u0417\u0430\u043f\u0443\u0441\u043a: cd /opt/running-bot && venv/bin/python3 scripts/probe_notify_off.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import get_connection  # noqa: E402

FLAGS = [
    ("notify_interval", "\u0432\u0435\u0447\u0435\u0440 \u0432\u0442/\u043f\u0442"),
    ("notify_morning_interval", "\u0443\u0442\u0440\u043e \u0432\u0442/\u043f\u0442"),
    ("notify_long", "\u0432\u0435\u0447\u0435\u0440 \u043b\u043e\u043d\u0433\u0430"),
    ("notify_morning_long", "\u0443\u0442\u0440\u043e \u0432\u0441"),
]


def main():
    with get_connection() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(user_preferences)")}
        flags = [(c, t) for c, t in FLAGS if c in cols]
        sel = ", ".join(f"COALESCE(p.{c}, 1)" for c, _ in flags)
        rows = conn.execute(f"""
            SELECT COALESCE(u.username, u.name), {sel}
            FROM user_preferences p JOIN users u ON u.id = p.user_id
            WHERE COALESCE(p.is_active, 1) = 1
        """).fetchall()

    print(f"\u0410\u043a\u0442\u0438\u0432\u043d\u044b\u0445 \u0441 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430\u043c\u0438: {len(rows)}\n")
    for i, (col, title) in enumerate(flags):
        off = [r[0] for r in rows if not r[i + 1]]
        print(f"{title:<16} \u0432\u044b\u043a\u043b\u044e\u0447\u0438\u043b\u0438: {len(off)}"
              + (f" \u2014 {', '.join(str(x) for x in off)}" if off else ""))

    any_off = [r for r in rows if not all(r[1:])]
    print(f"\n\u0412\u0441\u0435\u0433\u043e \u043b\u044e\u0434\u0435\u0439 \u0441 \u043e\u0442\u043a\u043b\u044e\u0447\u0451\u043d\u043d\u044b\u043c\u0438 \u0433\u0430\u043b\u043e\u0447\u043a\u0430\u043c\u0438: {len(any_off)}")
    for r in any_off:
        offs = [t for i, (_, t) in enumerate(flags) if not r[i + 1]]
        print(f"   {r[0]}: \u0432\u044b\u043a\u043b {', '.join(offs)}")


if __name__ == "__main__":
    main()
