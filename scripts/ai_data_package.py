"""Отладочный прогон пакета данных для ИИ на произвольной DD-тренировке (read-only).

Тонкая обёртка над src/ai_package.build_package (единый источник логики — он же
используется командой бота /ai). Печатает пакет в stdout; с флагом -p/--prompt
добавляет инструкцию-промпт перед данными (готово к вставке в ИИ).

Пишет в базу: НЕТ. Может обновить токен Garmin при реавторизации — штатно.
Импортирует: ai_package (→ garmin, database, activity_review). НЕ импортирует bot.py.

Запуск (uid по умолчанию 2 = Anton):
    venv/bin/python3 scripts/ai_data_package.py                 # последняя DD
    venv/bin/python3 scripts/ai_data_package.py DD_20260612     # по маске
    venv/bin/python3 scripts/ai_data_package.py 23219097987     # по activityId
    venv/bin/python3 scripts/ai_data_package.py DD_20260609 4   # маска + uid
    venv/bin/python3 scripts/ai_data_package.py DD_20260609 -p  # с промптом для ИИ
"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_package import build_package, PROMPT


async def main():
    args = list(sys.argv[1:])
    with_prompt = False
    for flag in ("-p", "--prompt"):
        if flag in args:
            args.remove(flag)
            with_prompt = True
    selector = args[0] if len(args) > 0 else None
    uid = int(args[1]) if len(args) > 1 else 2

    res = await build_package(uid, selector)
    if not res.get("ok"):
        print(f"⚠️ {res.get('msg')}")
        return
    if with_prompt:
        print(PROMPT)
        print()
    print(res["text"])


if __name__ == "__main__":
    asyncio.run(main())
