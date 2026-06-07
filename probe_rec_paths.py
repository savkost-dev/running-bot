"""Что в recovery dict у uid 2 при ПРОШЕДШЕЙ тренировке (force_fresh=False — путь unified_cache)
и при будущей (force_fresh=True). Печатаем ключи обоих."""
import asyncio, sys, json
sys.path.insert(0, '/opt/running-bot/src')
import bot

async def main():
    print("=== force_fresh=True (будущая) ===")
    r1 = await bot._get_unified_recovery(2, force_fresh=True)
    print(json.dumps(r1, ensure_ascii=False, default=str) if r1 else "None")
    print("\n=== force_fresh=False (прошедшая, unified_cache) ===")
    r2 = await bot._get_unified_recovery(2, force_fresh=False)
    print(json.dumps(r2, ensure_ascii=False, default=str) if r2 else "None")

asyncio.run(main())
