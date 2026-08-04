"""Кто и откуда пишет эталон в workout_templates.

Ищет по всем src/*.py вызовы save_workout_template / _save_workout_templates
и показывает, в какой функции каждый вызов находится.
Пишет в tmp/tmpl_writers.txt (utf-8). Запуск: python scripts/probe_tmpl_writers.py
"""
import os
import re

SRC = r"D:\running-bot\src"
OUT = r"D:\running-bot\tmp\tmpl_writers.txt"
pat = re.compile(r"save_workout_template|_save_workout_templates|workout_templates")

with open(OUT, "w", encoding="utf-8") as out:
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
                kind = "ОПРЕДЕЛЕНИЕ" if m else "вызов/упоминание"
                out.write(f"{fn}:{i} [{func}] {kind}: {line.strip()[:120]}\n")
print(f"OK -> {OUT}")
