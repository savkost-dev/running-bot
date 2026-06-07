"""П.2: принудительный синк свежего сырья + run_normalization по всем юзерам.
П.3: показ TR/BB из unified_cache по всем пользователям после."""
import asyncio, sys, json
sys.path.insert(0, '/opt/running-bot/src')
import database as db
from database import get_all_users, get_or_create_user, get_unified_data
from data_normalizer import run_normalization, UnifiedUserData
import bot

async def main():
    users = get_all_users()
    print("=== П.2: синк свежего сырья + нормализация ===")
    for telegram_id, name, _ in users:
        uid = get_or_create_user(telegram_id, name)
        if not bot._night_services(uid):
            continue
        try:
            await bot._sync_night_services(uid)
        except Exception as e:
            print(f"  uid {uid}: sync error {e}")
        try:
            run_normalization(uid)
        except Exception as e:
            print(f"  uid {uid}: norm error {e}")
        await asyncio.sleep(1)

    print("\n=== П.3: TR/BB из unified_cache по всем ===")
    for telegram_id, name, _ in users:
        uid = get_or_create_user(telegram_id, name)
        row = get_unified_data(uid, max_age_hours=240)
        if not row:
            continue
        u = UnifiedUserData.from_json(row["unified_json"])
        tr = u.s3_training_readiness
        tr_score = tr.get("score") if isinstance(tr, dict) else tr
        print(f"uid {uid:>3} {name:<16} TR={tr_score} BB={u.s3_body_battery} "
              f"src={row['sources']} upd={row['updated_at']}")

asyncio.run(main())
