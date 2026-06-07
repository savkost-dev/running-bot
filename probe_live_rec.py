"""Что реально в recovery-dict на лету (force_fresh=True) у админа uid 2 и Garmin-юзера.
Печатаем ключи и значения — чтобы знать что брать для 'текущих данных' в сообщении админу."""
import asyncio, sys, json
sys.path.insert(0, '/opt/running-bot/src')
import bot

async def main():
    for uid in (2, 8):
        print(f"\n=== uid {uid} _get_recovery_data(force_fresh=True) ===")
        rec = await bot._get_recovery_data(uid, force_fresh=True)
        if not rec:
            print("  None")
            continue
        for k, v in rec.items():
            print(f"  {k} = {json.dumps(v, ensure_ascii=False)[:120] if not isinstance(v,(int,float,str,type(None))) else v}")

asyncio.run(main())
