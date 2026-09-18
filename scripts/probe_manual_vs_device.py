"""Кто с трекером сидит на ручном якоре: что у него сейчас и что будет от данных часов.
Считает функциями zones.py (resolve_anchor / _zones_from_vdot), не своей формулой."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from database import get_connection, get_user_profile  # noqa: E402
import zones  # noqa: E402


def _z(profile):
    a = zones.resolve_anchor(profile)
    if not a:
        return None
    z = zones._zones_from_vdot(a["vdot"])
    return {"vdot": a["vdot"], "kind": a["kind"], "T": z.get("threshold"), "I": z.get("interval"),
            "R": z.get("repetition")}


def _sec(p):
    return zones._pace_to_sec_per_km(p) if p else None


conn = get_connection()
cur = conn.cursor()
users = {r[0]: (r[1], r[2]) for r in cur.execute("select id, name, username from users")}
services = {}
_tab = next((r[0] for r in cur.execute("select name from sqlite_master where type='table' and name like '%token%'")), None)
if not _tab:
    print("таблицы:", [r[0] for r in cur.execute("select name from sqlite_master where type='table'")])
    raise SystemExit("не нашёл таблицу токенов")
_cols = [r[1] for r in cur.execute(f"pragma table_info({_tab})")]
_ucol = next(c for c in _cols if "user" in c)
_scol = next(c for c in _cols if "service" in c or "provider" in c)
for uid, svc in cur.execute(f"select {_ucol}, {_scol} from {_tab}"):
    services.setdefault(uid, []).append(svc)

rows = []
_pcols = [r[1] for r in cur.execute("pragma table_info(user_profile)")]
_pkey = next(c for c in _pcols if "user" in c)
for uid, svcs in sorted(services.items()):
    prof = get_user_profile(uid) or {}
    _raw = cur.execute(f"select * from user_profile where {_pkey}=?", (uid,)).fetchone()
    raw = dict(zip(_pcols, _raw)) if _raw else {}
    p = {**raw, **{k: v for k, v in prof.items() if k in ("lactate_source", "vo2max_source")}}
    src_lt = (prof.get("lactate_source") or "")
    src_vo = (prof.get("vo2max_source") or "")
    manual = src_lt == "manual" or (src_vo == "manual" and src_lt != "device")
    if not manual:
        continue
    now = _z(prof)
    dev_prof = {
        "lactate_threshold_pace": raw.get("lt_pace_device"),
        "lactate_source": raw.get("lt_device_source") or "device",
        "vo2max": raw.get("vo2max_device"),
        "vo2max_source": raw.get("vo2max_device_source") or "device",
    }
    dev = _z(dev_prof) if (raw.get("vo2max_device") or raw.get("lt_pace_device")) else None
    name, uname = users.get(uid, ("?", ""))
    rows.append((uid, name, uname, ",".join(svcs), p, now, dev))

print(f"с трекером на ручном якоре: {len(rows)}\n")
hdr = f"{'uid':>4} {'ник':<22} {'сервис':<20} | {'ручной ЛП (дата)':<24} {'ручной VO2 (дата)':<22} | {'СЕЙЧАС T/I (VDOT)':<22} | {'VO2 часы (дата)':<22} {'ЛП часы (дата)':<20} | {'СТАНЕТ T/I (VDOT)':<22} {'ΔT сек':>6}"
print(hdr)
print("-" * len(hdr))
for uid, name, uname, svcs, p, now, dev in rows:
    d = lambda k: (p.get(k) or "")[:10]  # noqa: E731
    lt_m = f"{p.get('lt_pace_manual') or '—'} ({d('lt_manual_at')})" if p.get("lt_pace_manual") else "—"
    vo_m = f"{p.get('vo2max_manual') or '—'} ({d('vo2max_manual_at')})" if p.get("vo2max_manual") else "—"
    vo_d = f"{p.get('vo2max_device')} ({d('vo2max_device_at')}, {p.get('vo2max_device_source') or ''})" if p.get("vo2max_device") else "—"
    lt_d = f"{p.get('lt_pace_device')} ({d('lt_device_at')})" if p.get("lt_pace_device") else "—"
    now_s = f"{now['T']}/{now['I']} ({now['vdot']:.1f})" if now else "—"
    dev_s = f"{dev['T']}/{dev['I']} ({dev['vdot']:.1f})" if dev else "нет данных с часов"
    dT = ""
    if now and dev and now.get("T") and dev.get("T"):
        dT = f"{int(round(_sec(dev['T']) - _sec(now['T']))):+d}"
    print(f"{uid:>4} {(name or '')[:22]:<22} {svcs[:20]:<20} | {lt_m:<24} {vo_m:<22} | {now_s:<22} | {vo_d:<22} {lt_d:<20} | {dev_s:<22} {dT:>6}")
