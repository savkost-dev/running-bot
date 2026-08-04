"""Карта процесса: кто читает и пишет ключевые артефакты пайплайна.

Для каждой таблицы/артефакта ищет вызовы save_*/get_* по всем src/*.py
и показывает функцию, в которой находится вызов.
Пишет в tmp/process_map_raw.txt (utf-8).
Запуск: python scripts/probe_process_map.py
"""
import os
import re

SRC = r"D:\running-bot\src"
OUT = r"D:\running-bot\tmp\process_map_raw.txt"

TARGETS = {
    "анонс/анализ": r"save_workout_analysis|get_workout_analysis|get_latest_workout_analysis",
    "эталоны": r"save_workout_template|get_workout_template|_save_workout_templates",
    "рекомендации (лог)": r"save_recommendation|get_recommendations_for_date",
    "уведомления": r"save_workout_notification|get_last_workout_notification",
    "кэш атлета": r"save_athlete_cache|get_athlete_cache|refresh_athlete_cache",
    "сырьё сервисов": r"save_raw_service_data|get_raw_service_data|fetch_raw",
    "unified": r"save_unified|get_unified|run_normalization",
    "разбор /report": r"build_package|build_report_card|_garmin_candidate|_strava_candidate",
    "экспорт в часы": r"build_garmin_from_analysis|build_garmin_interval_workout|upload_workout",
    "поимка анонса": r"find_next_workout|find_next_long_run|_autoanalyze_post|scheduled_new_workout_check",
}

with open(OUT, "w", encoding="utf-8") as out:
    for title, rx in TARGETS.items():
        pat = re.compile(rx)
        out.write(f"\n===== {title} =====\n")
        for fn in sorted(os.listdir(SRC)):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(SRC, fn), encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            func = "<модуль>"
            for i, line in enumerate(lines, 1):
                m = re.match(r"\s*(?:async\s+)?def\s+(\w+)", line)
                if m:
                    func = m.group(1)
                if pat.search(line):
                    tag = "DEF" if m else "   "
                    out.write(f"  {tag} {fn}:{i} [{func}] {line.strip()[:110]}\n")
print(f"OK -> {OUT}")
