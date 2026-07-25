"""Все места работы с workout_analysis. Запуск: python scripts/probe_grep2.py"""
import os, re

SRC = r"D:\running-bot\src"
pat = re.compile(r"workout_analysis|work_text|analyzed_json")
for fn in sorted(os.listdir(SRC)):
    if not fn.endswith(".py"):
        continue
    p = os.path.join(SRC, fn)
    with open(p, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if pat.search(line):
                print(f"{fn}:{i}: {line.rstrip()[:150]}")
