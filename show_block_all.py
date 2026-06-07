"""Вывод как в админском блоке по всем с сервисами:
снимок на утро (TR/BB/HRV/RHR/сон/подъём/снят) + 'последняя синхронизация' (TR/BB/data_fetched_at).
data_fetched_at: Garmin = wellnessEndTimeLocal из сырья, иначе синк из unified_cache.data_dates."""
import sys, json
sys.path.insert(0, '/opt/running-bot/src')
from database import (get_all_users, get_or_create_user, get_token,
                      get_morning_caught, get_unified_data, get_raw_service_data)
from data_normalizer import UnifiedUserData

def has_services(uid):
    return any(get_token(uid, s) for s in ("garmin", "coros", "polar", "whoop"))

def garmin_obs_end(uid):
    if not get_token(uid, "garmin"):
        return None
    row = get_raw_service_data(uid, "garmin")
    if not row:
        return None
    try:
        us = (json.loads(row["raw_json"]) or {}).get("user_summary") or {}
        return us.get("wellnessEndTimeLocal") or None
    except Exception:
        return None

for telegram_id, name, _ in get_all_users():
    uid = get_or_create_user(telegram_id, name)
    if not has_services(uid):
        continue
    snap = get_morning_caught(uid) or {}
    row = get_unified_data(uid, max_age_hours=240)
    if row:
        u = UnifiedUserData.from_json(row["unified_json"])
        tr = u.s3_training_readiness
        c_tr = tr.get("score") if isinstance(tr, dict) else tr
        c_bb = u.s3_recovery_daily
        dd = u.data_dates or {}
        sync = (dd.get("garmin_synced_at") or dd.get("garmin_fetched")
                or dd.get("coros_fetched") or dd.get("polar_fetched")
                or dd.get("strava_fetched") or row.get("updated_at"))
    else:
        c_tr = c_bb = sync = None
    obs = garmin_obs_end(uid)
    data_at = obs or sync

    print(f"=== uid {uid} {name} ===")
    if snap.get("caught"):
        print(f"  Снимок на утро ({snap.get('date')}, снят {snap.get('snapshot_at')}): "
              f"TR {snap.get('tr')} | BB {snap.get('bb')} | HRV {snap.get('hrv')} | "
              f"RHR {snap.get('rhr')} | сон {snap.get('sleep_h')}ч | подъём {snap.get('wake_at')}")
    else:
        print("  Снимок на утро: нет (ночь не поймана)")
    print(f"  Последняя синхронизация ({data_at}): TR {c_tr} | BB {c_bb}")
