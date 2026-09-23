"""probe_strava_candidates.py — разведка (20.09.2026): кого позвать в Strava (лимит 10 подключений).

Только читает ЛОКАЛЬНУЮ копию базы data/running_bot.db. Кандидат = активный пользователь,
у кого есть трекер (user_tokens), но НЕТ strava. Активность за последние N дней (по умолчанию 14)
считается по следам в базе: оценки (recommendation_ratings: report / recommendation),
фидбек по темпу (pace_feedback), полученные рекомендации (recommendation_history).
Сортировка: сначала оценившие разбор (/report), потом остальные по числу следов.

Запуск локально:  python scripts\\probe_strava_candidates.py [дней]
bot.py не импортирует.
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "running_bot.db"


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def main(days):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    since = f"-{days} days"

    active = {r[0] for r in conn.execute(
        "SELECT u.id FROM users u LEFT JOIN user_preferences p ON u.id = p.user_id "
        "WHERE p.is_active IS NULL OR p.is_active = 1")}
    services = {}
    for uid, svc in conn.execute("SELECT user_id, service FROM user_tokens"):
        services.setdefault(uid, set()).add(svc)
    cand = [u for u in active if u in services and "strava" not in services[u]]

    ucols = _cols(conn, "users")
    name_col = "username" if "username" in ucols else None
    full_col = next((c for c in ("full_name", "first_name", "name") if c in ucols), None)
    names = {r["id"]: (r[name_col] if name_col else None, r[full_col] if full_col else None)
             for r in conn.execute("SELECT * FROM users")}

    rep, rec_rate, fb, reco = {}, {}, {}, {}
    rcols = _cols(conn, "recommendation_ratings")
    kind_expr = "kind" if "kind" in rcols else "'recommendation'"
    for uid, kind, n in conn.execute(
            f"SELECT user_id, {kind_expr}, COUNT(*) FROM recommendation_ratings "
            f"WHERE created_at >= datetime('now', ?) GROUP BY user_id, {kind_expr}", (since,)):
        (rep if kind == "report" else rec_rate)[uid] = n
    try:
        for uid, n in conn.execute(
                "SELECT user_id, COUNT(*) FROM pace_feedback "
                "WHERE created_at >= datetime('now', ?) GROUP BY user_id", (since,)):
            fb[uid] = n
    except sqlite3.OperationalError:
        pass
    try:
        for uid, n in conn.execute(
                "SELECT user_id, COUNT(*) FROM recommendation_history "
                "WHERE workout_date >= date('now', ?) GROUP BY user_id", (since,)):
            reco[uid] = n
    except sqlite3.OperationalError:
        pass

    rows = []
    for u in cand:
        score = rep.get(u, 0) * 10 + rec_rate.get(u, 0) * 3 + fb.get(u, 0) * 3 + reco.get(u, 0)
        rows.append((score, u))
    rows.sort(reverse=True)

    print(f"Активных с трекером без Strava: {len(cand)}; следы за {days} дн. "
          f"(разбор×10 + оценка×3 + фидбек×3 + рекомендации×1)")
    print(f"{'uid':>4}  {'@username':<22} {'имя':<22} {'трекеры':<14} {'разб':>4} {'оцен':>4} {'фидб':>4} {'реко':>4} {'балл':>4}")
    for score, u in rows:
        un, fn = names.get(u, (None, None))
        print(f"{u:>4}  {('@' + un) if un else '—':<22} {(fn or '—')[:22]:<22} "
              f"{','.join(sorted(services[u])):<14} {rep.get(u, 0):>4} {rec_rate.get(u, 0):>4} "
              f"{fb.get(u, 0):>4} {reco.get(u, 0):>4} {score:>4}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 14)
