"""Снимок на утро (БД) + текущий кэш (unified_cache) рядом по всем с сервисами."""
import sys
sys.path.insert(0, '/opt/running-bot/src')
from database import get_all_users, get_or_create_user, get_morning_caught, get_unified_data
from data_normalizer import UnifiedUserData

def has_services(uid):
    from database import get_token
    return any(get_token(uid, s) for s in ("garmin", "coros", "polar", "whoop"))

print(f"{'uid':>3} {'имя':<16} | {'СНИМОК НА УТРО':<20} | {'КЭШ (сейчас)'}")
print(f"{'':>3} {'':<16} | {'TR / BB':<20} | {'TR / BB'}")
print("-" * 70)
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
    else:
        c_tr = c_bb = None
    snap_str = f"TR {snap.get('tr')} / BB {snap.get('bb')}" if snap.get('caught') else "нет снимка"
    cache_str = f"TR {c_tr} / BB {c_bb}"
    print(f"{uid:>3} {name[:16]:<16} | {snap_str:<20} | {cache_str}")
