"""Ищем цепочку экспорта воркаута в Garmin: кто строит шаги и откуда берёт темпы.
Пишет в tmp/garmin_export_hits.txt (utf-8). Запуск: python scripts/probe_garmin_export2.py"""
import os
import re

SRC = r"D:\running-bot\src"
OUT = r"D:\running-bot\tmp\garmin_export_hits.txt"
pat = re.compile(r"skipLastRestStep|upload_workout|garmin_workout|_lvl|workoutSegments|targetValue", re.IGNORECASE)
with open(OUT, "w", encoding="utf-8") as out:
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py"):
            continue
        path = os.path.join(SRC, fn)
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if pat.search(line):
                    out.write(f"{fn}:{i}: {line.rstrip()[:150]}\n")
print(f"OK -> {OUT}")
