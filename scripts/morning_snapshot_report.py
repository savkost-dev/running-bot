"""Утренний слепок (morning snapshot) по всем пользователям — read-only.

Показывает на юзера 2 строки:
  утро   — замороженный снимок (get_morning_caught): TR/BB/HRV/RHR/сон/подъём
  сейчас — свежие данные из unified_cache.unified_json: текущие TR и BB

Та же строка unified_cache хранит И утренний снимок (morning_*), И свежий
unified_json (+ updated_at, sources). Устройство = sources. В БД не пишет.

Запуск на сервере:
    /opt/running-bot/venv/bin/python3 scripts/morning_snapshot_report.py            # на сегодня (МСК)
    /opt/running-bot/venv/bin/python3 scripts/morning_snapshot_report.py 2026-06-09 # конкретная дата
    /opt/running-bot/venv/bin/python3 scripts/morning_snapshot_report.py all         # все, независимо от даты
"""
import sys
import os
import json
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import get_users_list_for_b, get_morning_caught, get_token, get_connection

MSK = timezone(timedelta(hours=3))

_SVC_NAMES = {"garmin": "Garmin", "coros": "COROS", "polar": "Polar",
              "whoop": "Whoop", "strava": "Strava"}
_NIGHT_SVCS = ("garmin", "coros", "polar", "whoop")


def _num(v):
    """37.0 → '37', 8.12 → '8.1', None → '—'."""
    if v is None:
        return "—"
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else f"{v:.1f}"
    return str(v)


def _hhmm(ts, to_msk=False):
    if not ts:
        return "—"
    s = str(ts)
    if to_msk:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(MSK).strftime("%H:%M")
        except Exception:
            return "—"
    return s[11:16] if len(s) >= 16 else s


def _night_devices(db_user_id):
    """Подключённые ночные сервисы юзера (по токенам)."""
    return [s for s in _NIGHT_SVCS if get_token(db_user_id, s)]


def _current(db_user_id):
    """Свежие TR/BB из unified_cache.unified_json. Возвращает (tr, bb, updated_at, sources)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT unified_json, sources, updated_at FROM unified_cache WHERE user_id = ?",
            (db_user_id,)
        ).fetchone()
    if not row or not row[0]:
        return None, None, (row[2] if row else None), (row[1] if row else "")
    try:
        u = json.loads(row[0])
    except Exception:
        return None, None, row[2], row[1] or ""
    tr_obj = u.get("s3_training_readiness") or {}
    tr = tr_obj.get("score") if isinstance(tr_obj, dict) else None
    bb = u.get("s3_recovery_daily")  # универсальное суточное: BB/recoveryPct/ANS
    return tr, bb, row[2], (row[1] or ",".join(u.get("sources") or []))


def _age_str(updated_at):
    if not updated_at:
        return ""
    try:
        d = datetime.fromisoformat(str(updated_at))
        hrs = (datetime.now() - d).total_seconds() / 3600
        return f"{int(hrs)}ч назад" if hrs >= 1 else f"{int(hrs*60)}мин назад"
    except Exception:
        return ""


def build_report(target_date: str | None = None) -> str:
    today_msk = datetime.now(MSK).strftime("%Y-%m-%d")
    show_all = (target_date == "all")
    if not target_date or target_date == "today":
        target_date = today_msk

    users = get_users_list_for_b()
    caught, missed_with_dev, missed_no_dev = [], [], []

    for u in users:
        uid, name = u["db_user_id"], u["name"]
        snap = get_morning_caught(uid)
        devs = _night_devices(uid)
        if snap and snap.get("caught") and (show_all or snap.get("date") == target_date):
            caught.append((name, snap, devs, uid))
        elif devs:
            missed_with_dev.append((name, devs))
        else:
            missed_no_dev.append(name)

    hdr = "все даты" if show_all else target_date
    lines = [f"🔬 Утренний слепок — {hdr} · поймано {len(caught)}/{len(users)}", ""]

    for name, s, devs, uid in caught:
        dev_str = "+".join(_SVC_NAMES.get(d, d) for d in devs) or "—"
        date_tag = f" [{s.get('date')}]" if show_all else ""
        cur_tr, cur_bb, upd, _ = _current(uid)
        lines.append(f"▸ {name} · {dev_str}{date_tag}")
        lines.append(
            f"   утро:   TR {_num(s.get('tr'))} | BB {_num(s.get('bb'))} | "
            f"HRV {_num(s.get('hrv'))} | RHR {_num(s.get('rhr'))} | "
            f"сон {_num(s.get('sleep_h'))}ч | подъём {_hhmm(s.get('wake_at'))} "
            f"(снят {_hhmm(s.get('snapshot_at'), to_msk=True)})"
        )
        age = _age_str(upd)
        age_tag = f"  (обновл. {_hhmm(upd)} {age})".rstrip() if upd else ""
        lines.append(f"   сейчас: TR {_num(cur_tr)} | BB {_num(cur_bb)}{age_tag}")

    if missed_with_dev:
        lines.append("")
        lines.append(f"⚠️ Ночной трекер есть, но не поймано ({len(missed_with_dev)}):")
        for name, devs in missed_with_dev:
            lines.append(f"   {name} · {'+'.join(_SVC_NAMES.get(d, d) for d in devs)}")

    if missed_no_dev:
        lines.append("")
        lines.append(f"Без ночного трекера ({len(missed_no_dev)}): не ожидается слепок")

    return "\n".join(lines)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    print(build_report(arg))
