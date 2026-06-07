"""П.2: принудительная перенормализация unified_cache по всем (без синка живого).
П.3: показ TR (s3_training_readiness) и BB в широком смысле (s3_recovery_daily) по всем."""
import sys
sys.path.insert(0, '/opt/running-bot/src')
from database import get_all_users, get_or_create_user, get_unified_data
from data_normalizer import run_normalization, UnifiedUserData

print("=== Перенормализация unified_cache (на текущем сырье) ===")
for telegram_id, name, _ in get_all_users():
    uid = get_or_create_user(telegram_id, name)
    try:
        run_normalization(uid)
    except Exception as e:
        print(f"uid {uid} {name}: norm error {e}")

print("\n=== unified_cache: TR + BB(в широком смысле, s3_recovery_daily) ===")
for telegram_id, name, _ in get_all_users():
    uid = get_or_create_user(telegram_id, name)
    row = get_unified_data(uid, max_age_hours=240)
    if not row:
        continue
    u = UnifiedUserData.from_json(row["unified_json"])
    tr = u.s3_training_readiness
    tr_score = tr.get("score") if isinstance(tr, dict) else tr
    print(f"uid {uid:>3} {name:<16} src={row['sources']:<20} "
          f"TR={tr_score} BB={u.s3_recovery_daily}")
