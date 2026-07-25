"""Пробник карточки разбора (build_report_card) — read-only к боевым веткам.

Собирает данные тренировки через ai_package.build_package (Garmin/Strava,
как в /report), затем строит НОВУЮ карточку-таблицу build_report_card
(шапка + зебра + заливка отклонений + строка «Среднее» + подпись бота)
и сохраняет PNG рядом. Боевые графики не трогает, в бота не встроено.

Пишет в базу: НЕТ. Пишет файл: card_probe_<uid>.png (в текущей папке).
Импортирует: ai_package, database, activity_review. НЕ импортирует bot.py.

Запуск:
    venv/bin/python3 scripts/probe_report_card.py                # uid=2, последняя DD
    venv/bin/python3 scripts/probe_report_card.py 2 18886572975  # uid + селектор
"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ai_package


async def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    selector = sys.argv[2] if len(sys.argv) > 2 else None

    g = await ai_package._garmin_candidate(uid, selector)
    s = await ai_package._strava_candidate(uid, selector)
    cand = ai_package._choose_candidate(g, s)
    if not cand:
        print("DD-активность не найдена (Garmin/Strava).")
        return
    print(f"Активность: {cand['name']} ({cand['source']}, {cand['wdate']}, группа {cand['wgroup']})")

    s4 = ai_package._s4_by_date(cand["wdate"], cand["wtype_key"])
    out = await ai_package.build_report_card(
        cand["splits"], cand["plan_steps"], cand["name"],
        cand["wdate"], cand["wgroup"], cand["source"],
        s4, ".", f"probe_{uid}", dark=False)
    print(f"Карточка: {out or 'не построилась (нет work-лэпов?)'}")
    charts = await ai_package.build_charts_stacked(
        cand["splits"], cand["plan_steps"], cand["name"],
        ".", f"probe_{uid}", dark=False)
    print(f"Графики (одна картинка): {charts or 'не построились'}")


if __name__ == "__main__":
    asyncio.run(main())
