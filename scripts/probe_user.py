"""\u041a\u0430\u0440\u0442\u043e\u0447\u043a\u0430 \u043e\u0434\u043d\u043e\u0433\u043e \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f \u043f\u043e username/\u0438\u043c\u0435\u043d\u0438 (20.08.2026, \u0442\u043e\u043b\u044c\u043a\u043e \u0447\u0442\u0435\u043d\u0438\u0435).

\u041f\u0435\u0447\u0430\u0442\u0430\u0435\u0442 \u0432\u0441\u0435 \u0417\u0410\u041f\u041e\u041b\u041d\u0415\u041d\u041d\u042b\u0415 \u043f\u043e\u043b\u044f \u0438\u0437 users / user_profile / user_preferences,
\u0447\u0443\u0432\u0441\u0442\u0432\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0435 (\u043f\u0430\u0440\u043e\u043b\u0438/\u0442\u043e\u043a\u0435\u043d\u044b) \u043c\u0430\u0441\u043a\u0438\u0440\u0443\u044e\u0442\u0441\u044f.

\u0417\u0430\u043f\u0443\u0441\u043a: cd /opt/running-bot && venv/bin/python3 scripts/probe_user.py Sp1r1donSunR0tAtor
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import get_connection  # noqa: E402

SECRET = ("password", "token", "secret", "refresh")


def show(conn, table, user_id):
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except Exception:
        return
    if not cols:
        return
    key = "id" if table == "users" else "user_id"
    row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (user_id,)).fetchone()
    if not row:
        print(f"\n[{table}] \u0437\u0430\u043f\u0438\u0441\u0438 \u043d\u0435\u0442")
        return
    print(f"\n[{table}]")
    for name, val in zip(cols, row):
        if val is None or val == "":
            continue
        if any(s in name.lower() for s in SECRET):
            val = "***"
        print(f"   {name}: {str(val)[:120]}")


def main():
    who = (sys.argv[1] if len(sys.argv) > 1 else "").lstrip("@")
    if not who:
        print("\u0424\u043e\u0440\u043c\u0430\u0442: probe_user.py <username|\u0438\u043c\u044f>")
        return
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, telegram_id, name, username FROM users "
            "WHERE username = ? COLLATE NOCASE OR name LIKE ?",
            (who, f"%{who}%")).fetchone()
        if not row:
            print(f"\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c {who} \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d")
            return
        uid, tid, name, uname = row
        print(f"uid={uid} telegram_id={tid} \u0438\u043c\u044f={name} @{uname}")
        for t in ("users", "user_profile", "user_preferences", "athlete_cache",
                  "unified_cache", "garmin_recovery_cache", "last_recommendation"):
            show(conn, t, uid)


if __name__ == "__main__":
    main()
