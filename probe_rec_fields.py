"""Показ всех полей восстановления из unified_cache по всем юзерам:
s3_training_readiness (TR), s3_body_battery (Garmin суточное),
s3_coros_recovery (COROS суточное), s3_recovery_daily (Polar суточное)."""
import sys
sys.path.insert(0, '/opt/running-bot/src')
from database import get_all_users, get_or_create_user, get_unified_data
from data_normalizer import UnifiedUserData

for telegram_id, name, _ in get_all_users():
    uid = get_or_create_user(telegram_id, name)
    row = get_unified_data(uid, max_age_hours=240)
    if not row:
        continue
    u = UnifiedUserData.from_json(row["unified_json"])
    tr = u.s3_training_readiness
    tr_score = tr.get("score") if isinstance(tr, dict) else tr
    print(f"uid {uid:>3} {name:<16} src={row['sources']:<20} "
          f"TR={tr_score} BB={u.s3_body_battery} "
          f"coros_rec={u.s3_coros_recovery} polar_daily={u.s3_recovery_daily}")
