"""\u041f\u043e\u0447\u0435\u043c\u0443 \u0443 \u043d\u0435\u043a\u043e\u0442\u043e\u0440\u044b\u0445 evening_recovery_score \u043f\u043e\u0447\u0442\u0438 \u043d\u043e\u043b\u044c (19.08.2026, \u0442\u043e\u043b\u044c\u043a\u043e \u0447\u0442\u0435\u043d\u0438\u0435).

\u041f\u043e \u0440\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0430\u0446\u0438\u044f\u043c \u0437\u0430 \u0434\u0430\u0442\u0443 \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442: \u0433\u0440\u0443\u043f\u043f\u0430, rec-score \u0438 \u0447\u0442\u043e \u0443 \u0447\u0435\u043b\u043e\u0432\u0435\u043a\u0430 \u0435\u0441\u0442\u044c
\u0438\u0437 \u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a\u043e\u0432 (garmin/coros/polar/whoop \u043f\u043e \u043a\u0440\u0435\u0434\u0430\u043c, strava \u043f\u043e athlete_id).
\u041a\u043e\u043b\u043e\u043d\u043a\u0438 \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u044f\u044e\u0442\u0441\u044f \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438 \u2014 \u0441\u0445\u0435\u043c\u0430 \u043c\u0435\u043d\u044f\u043b\u0430\u0441\u044c.

\u0417\u0430\u043f\u0443\u0441\u043a: cd /opt/running-bot && venv/bin/python3 scripts/probe_recovery_low.py 2026-08-18
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import get_connection  # noqa: E402

CANDIDATES = ["garmin_email", "coros_email", "polar_user_id", "whoop_user_id",
              "strava_athlete_id"]


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else ""
    if not date:
        print("\u0424\u043e\u0440\u043c\u0430\u0442: probe_recovery_low.py 2026-08-18")
        return

    with get_connection() as conn:
        def cols(table):
            try:
                return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            except Exception:
                return set()

        prof_cols, user_cols = cols("user_profile"), cols("users")
        rec_cols = cols("last_recommendation")
        rec_score = "r.evening_recovery_score" if "evening_recovery_score" in rec_cols else "NULL"
        rec_low = "r.lowered_by_recovery" if "lowered_by_recovery" in rec_cols else "0"
        found = {}
        for c in CANDIDATES:
            if c in prof_cols:
                found[c] = "p"
            elif c in user_cols:
                found[c] = "u"
        print("\u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a\u0438 \u0432 \u0441\u0445\u0435\u043c\u0435:", ", ".join(f"{k}({v})" for k, v in found.items()) or "\u043d\u0435\u0442")

        sel = "".join(
            f", (CASE WHEN {t}.{c} IS NOT NULL AND {t}.{c} != '' THEN 1 ELSE 0 END)"
            for c, t in found.items())
        rows = conn.execute(f"""
            SELECT u.id, COALESCE(u.username, u.name),
                   r.recommended_group, {rec_score},
                   {rec_low}{sel}
            FROM last_recommendation r
            JOIN users u ON u.id = r.user_id
            LEFT JOIN user_profile p ON p.user_id = u.id
            WHERE r.workout_date = ?
            ORDER BY ({rec_score} IS NULL), {rec_score}
        """, (date,)).fetchall()

    names = list(found.keys())
    print(f"\n\u0420\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0430\u0446\u0438\u0439 \u0437\u0430 {date}: {len(rows)}\n")
    for row in rows:
        uid, who, grp, rec, lowered = row[:5]
        src = [names[i].split("_")[0] for i, v in enumerate(row[5:]) if v]
        mark = " \u2193" if lowered else ""
        print(f"{uid:>4} {str(who)[:26]:<26} \u0433\u0440{str(grp):<4} "
              f"rec={('\u2014' if rec is None else rec)}{mark}  {', '.join(src) or '\u043d\u0435\u0442 \u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a\u043e\u0432'}")


if __name__ == "__main__":
    main()
