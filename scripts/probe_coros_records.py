"""Разведчик: сырой список тренировок COROS (MCP) за окно дат.

Печатает ответ querySportRecords как есть — смотрим, есть ли в нём
пользовательское имя тренировки (маска DD_...).
Запуск на сервере:
    cd /opt/running-bot && venv/bin/python scripts/probe_coros_records.py 75 20260904 20260904
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import coros_mcp


async def main():
    if len(sys.argv) < 4:
        print("usage: probe_coros_records.py <db_user_id> <startDate> <endDate>")
        return
    uid = int(sys.argv[1])
    start_date = sys.argv[2]
    end_date = sys.argv[3]

    async with coros_mcp._connect(uid) as (session, token):
        if not session:
            print("ПУСТО: связь не поднялась (нет токена или initialize не прошёл)")
            return
        text = await coros_mcp._call_tool(
            session, token, "querySportRecords",
            {"startDate": start_date, "endDate": end_date, "limit": 20}, 2)

    if not text:
        print("ПУСТО: инструмент вернул пусто")
        return
    print(f"ДЛИНА ОТВЕТА: {len(text)} знаков")
    print("=" * 60)
    print(text)


if __name__ == "__main__":
    asyncio.run(main())
