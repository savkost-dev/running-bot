"""Тест публикации брифа комментарием под анонсом (от аккаунта Telethon-сессии).

Берёт последний валидный интервальный анализ, рендерит бриф из кэша режимов
и постит комментарием под соответствующим постом канала — тем же кодом,
что вечерняя рассылка. Без --apply только печатает, что и куда ушло бы.

⚠ С --apply комментарий появится в ЖИВОМ чате клуба от имени владельца сессии.

Запуск (на сервере):
  venv/bin/python3 scripts/probe_post_comment.py           # показать
  venv/bin/python3 scripts/probe_post_comment.py --apply   # опубликовать
"""
import asyncio
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

APPLY = "--apply" in sys.argv


async def main():
    conn = sqlite3.connect("running_bot.db")
    row = conn.execute(
        "SELECT post_id, workout_date, analyzed_json FROM workout_analysis "
        "WHERE is_valid = 1 AND workout_type = 'interval' "
        "ORDER BY workout_date DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        print("нет валидного интервального анализа")
        return
    post_id, wdate, ajson = row
    result = json.loads(ajson or "{}")
    modes = result.get("modes")
    if not modes:
        print(f"анализ за {wdate} без режимов — постить нечего")
        return

    import announce_brief
    text = announce_brief.format_brief(result, modes)
    # Тот же хвост, что добавляет боевая рассылка — тест эквивалентен боевому пути
    text += ("\n\n🤖 Персональная группа под твою форму, тренировка в часы "
             "и разбор после финиша — @DD_adviser_bot")
    print(f"пост: {post_id} · дата тренировки: {wdate} · длина брифа: {len(text)}")
    print("─" * 40)
    print(text[:600] + ("…" if len(text) > 600 else ""))
    print("─" * 40)

    if not APPLY:
        print("Просмотр. Для публикации добавь --apply")
        return
    import telegram_reader
    ok = await telegram_reader.post_comment(post_id, text)
    print("ОПУБЛИКОВАНО ✅" if ok else "НЕ ОПУБЛИКОВАНО ❌ (см. вывод выше)")
    await telegram_reader.close_client()


asyncio.run(main())
