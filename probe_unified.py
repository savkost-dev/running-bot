"""Что в unified_cache (слой 2) у uid 2: s3_-поля восстановления + когда обновлялось.
Почему recovery_daily/training_readiness/recovery_total пустые."""
import sys, json
sys.path.insert(0, '/opt/running-bot/src')
from database import get_unified_data
from data_normalizer import UnifiedUserData

row = get_unified_data(2, max_age_hours=240)
if not row:
    print("unified_cache: НЕТ строки (или старше 240ч)")
else:
    print("updated_at:", row["updated_at"])
    print("sources:", row["sources"])
    u = UnifiedUserData.from_json(row["unified_json"])
    print("s3_recovery_daily:", getattr(u, "s3_recovery_daily", "НЕТ ПОЛЯ"))
    print("s3_training_readiness:", getattr(u, "s3_training_readiness", "НЕТ ПОЛЯ"))
    print("s3_recovery_total:", getattr(u, "s3_recovery_total", "НЕТ ПОЛЯ"))
    print("s3_hrv:", getattr(u, "s3_hrv", "НЕТ ПОЛЯ"))
    print("s3_rhr:", getattr(u, "s3_rhr", "НЕТ ПОЛЯ"))
    print("data_dates:", getattr(u, "data_dates", "НЕТ ПОЛЯ"))
    # весь json для полноты
    print("\n--- весь unified_json ---")
    print(row["unified_json"][:1500])
