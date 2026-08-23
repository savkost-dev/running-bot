"""\u041a\u0442\u043e \u0447\u0442\u043e \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u043b \u0438 \u043a\u0442\u043e \u0440\u0435\u0430\u043b\u044c\u043d\u043e \u043e\u0442\u0434\u0430\u0451\u0442 \u0434\u0430\u043d\u043d\u044b\u0435 (21.08.2026, \u0442\u043e\u043b\u044c\u043a\u043e \u0447\u0442\u0435\u043d\u0438\u0435).

\u041f\u043e \u043a\u0430\u0436\u0434\u043e\u043c\u0443 \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u043c\u0443: \u043a\u0430\u043a\u0438\u0435 \u0441\u0435\u0440\u0432\u0438\u0441\u044b \u043f\u0440\u0438\u0432\u044f\u0437\u0430\u043d\u044b, \u0435\u0441\u0442\u044c \u043b\u0438 \u0441\u0432\u0435\u0436\u0438\u0435 \u0434\u0430\u043d\u043d\u044b\u0435
\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f (unified_cache / garmin_recovery_cache / \u0443\u0442\u0440\u0435\u043d\u043d\u0438\u0439 \u0441\u043d\u0438\u043c\u043e\u043a),
\u0438 \u043e\u0442\u043a\u0443\u0434\u0430 \u0432\u0437\u044f\u0442 \u044f\u043a\u043e\u0440\u044c \u0437\u043e\u043d.

\u0417\u0430\u043f\u0443\u0441\u043a: cd /opt/running-bot && venv/bin/python3 scripts/probe_sources.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import get_connection  # noqa: E402

CREDS = [("garmin_email", "Gar"), ("coros_email", "Cor"),
         ("polar_user_id", "Pol"), ("whoop_user_id", "Whp"),
         ("strava_athlete_id", "Str")]


def main():
    with get_connection() as conn:
        pcols = {r[1] for r in conn.execute("PRAGMA table_info(user_profile)")}
        ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        found = [(c, s) for c, s in CREDS if c in pcols or c in ucols]
        sel = "".join(
            f", (CASE WHEN {'p' if c in pcols else 'u'}.{c} IS NOT NULL "
            f"AND {'p' if c in pcols else 'u'}.{c} != '' THEN 1 ELSE 0 END)"
            for c, _ in found)

        rows = conn.execute(f"""
            SELECT COALESCE(u.username, u.name) AS who,
                   (SELECT COUNT(*) FROM unified_cache c WHERE c.user_id = u.id),
                   (SELECT COUNT(*) FROM garmin_recovery_cache g WHERE g.user_id = u.id),
                   (SELECT COUNT(*) FROM mornings m WHERE m.user_id = u.id),
                   (SELECT a.zones_source FROM athlete_cache a WHERE a.user_id = u.id)
                   {sel}
            FROM users u
            LEFT JOIN user_profile p ON p.user_id = u.id
            LEFT JOIN user_preferences pr ON pr.user_id = u.id
            WHERE COALESCE(pr.is_active, 1) = 1
            ORDER BY who COLLATE NOCASE
        """).fetchall()

    names = [s for _, s in found]
    with_data = 0
    print(f"{'\u043a\u0442\u043e':<24} {'\u0441\u0435\u0440\u0432\u0438\u0441\u044b':<18} {'unified':>7} {'gar_rec':>7} "
          f"{'\u0443\u0442\u0440\u0430':>5}  \u044f\u043a\u043e\u0440\u044c \u0437\u043e\u043d")
    for r in rows:
        who, uni, grec, morn, zsrc = r[:5]
        svc = " ".join(names[i] for i, v in enumerate(r[5:]) if v) or "\u2014"
        if uni or grec:
            with_data += 1
        print(f"{str(who)[:24]:<24} {svc:<18} {uni:>7} {grec:>7} {morn:>5}  {zsrc or '\u2014'}")

    print(f"\n\u0410\u043a\u0442\u0438\u0432\u043d\u044b\u0445: {len(rows)} \u00b7 \u0441 \u043a\u044d\u0448\u0435\u043c \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f: {with_data}")


if __name__ == "__main__":
    main()
