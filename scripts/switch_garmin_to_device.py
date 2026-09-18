"""Перевод пользователей Garmin с ручных якорей зон на данные часов.
Сухой прогон по умолчанию: показывает «было → стало» и ничего не пишет.
Запись только с аргументом apply."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from database import get_connection, get_user_profile  # noqa: E402
import zones  # noqa: E402

APPLY = len(sys.argv) > 1 and sys.argv[1] == "apply"

conn = get_connection()
cur = conn.cursor()
users = {r[0]: (r[1], r[2]) for r in cur.execute("select id, name, username from users")}
_tab = next(r[0] for r in cur.execute(
    "select name from sqlite_master where type='table' and name like '%token%'"))
_cols = [r[1] for r in cur.execute(f"pragma table_info({_tab})")]
_ucol = next(c for c in _cols if "user" in c)
_scol = next(c for c in _cols if "service" in c or "provider" in c)
garmin = {uid for uid, svc in cur.execute(f"select {_ucol}, {_scol} from {_tab}") if svc == "garmin"}

pcols = [r[1] for r in cur.execute("pragma table_info(user_profile)")]
pkey = next(c for c in pcols if "user" in c)


def zline(prof):
    a = zones.resolve_anchor(prof)
    if not a:
        return "—"
    z = zones._zones_from_vdot(a["vdot"])
    return f"{z.get('threshold')}/{z.get('interval')} (VDOT {a['vdot']:.1f}, {a['kind']})"


todo = []
for uid in sorted(garmin):
    prof = get_user_profile(uid) or {}
    if (prof.get("lactate_source") or "") != "manual" and (prof.get("vo2max_source") or "") != "manual":
        continue
    row = cur.execute(f"select * from user_profile where {pkey}=?", (uid,)).fetchone()
    raw = dict(zip(pcols, row)) if row else {}
    if not (raw.get("vo2max_device") or raw.get("lt_pace_device")):
        continue
    after = {
        "lactate_threshold_pace": raw.get("lt_pace_device"),
        "lactate_source": raw.get("lt_device_source") or "device",
        "vo2max": raw.get("vo2max_device"),
        "vo2max_source": raw.get("vo2max_device_source") or "device",
    }
    todo.append((uid, prof, after))

print(("ЗАПИСЬ" if APPLY else "СУХОЙ ПРОГОН") + f": Garmin на ручных якорях — {len(todo)} чел.\n")
for uid, prof, after in todo:
    name, uname = users.get(uid, ("?", ""))
    print(f"{uid:>4} {(name or '')[:22]:<22} @{(uname or '-'):<20} {zline(prof):<34} → {zline(after)}")

if APPLY:
    for uid, _, _ in todo:
        cur.execute(
            "update user_profile set lt_priority='device', vo2max_priority='device', "
            "lactate_locked=0, vo2max_locked=0 where " + pkey + "=?", (uid,))
    conn.commit()
    print("\nприоритеты переключены, пересчитываю зоны…")
    for uid, _, _ in todo:
        z = zones.recalculate_and_save(uid)
        name = users.get(uid, ("?", ""))[0]
        print(f"  {uid:>4} {(name or '')[:22]:<22} {z['zones'].get('threshold') if z else '—'} "
              f"({z.get('source') if z else 'нет якоря'})")
