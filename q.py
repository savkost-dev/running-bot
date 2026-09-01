import asyncio, sys
sys.path.insert(0, "/opt/running-bot/src")
import telegram_reader as tr
async def main():
    cl = await tr._get_shared_client()
    n = 0
    async for m in cl.iter_messages(tr.CHANNEL, reply_to=2438, limit=300):
        n += 1
        t = (m.text or "").replace("\n", " ")
        if "876" in t or "1314" in t:
            print("НАЙДЕНО:", m.id, m.sender_id, t[:150])
    print("всего:", n)
asyncio.run(main())
