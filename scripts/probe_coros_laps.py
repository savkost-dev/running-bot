"""Разведчик: сырые круги одной COROS-тренировки (MCP).

Печатает ответ queryActivityLapData как есть, без разбора.
Запуск на сервере:
    cd /opt/running-bot && venv/bin/python scripts/probe_coros_laps.py 75 480097626216235010 103
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import coros_mcp


async def main():
    if len(sys.argv) < 4:
        print("usage: probe_coros_laps.py <db_user_id> <labelId> <sportType>")
        return
    uid = int(sys.argv[1])
    label_id = sys.argv[2]
    sport_type = int(sys.argv[3])

    text = await coros_mcp.fetch_lap_data(uid, label_id, sport_type)
    if text is None:
        print("ПУСТО: ответа нет (нет токена, связь не поднялась или инструмент вернул пусто)")
        return
    print(f"ДЛИНА ОТВЕТА: {len(text)} знаков")
    print("=" * 60)
    print(text)


if __name__ == "__main__":
    asyncio.run(main())
