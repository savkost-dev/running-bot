"""Где формируется строка «Работа:» рассылки. Запуск: python scripts\probe_grep.py"""
import os, re

SRC = r"D:\running-bot\src"
pat = re.compile(r"Работа|workout_summary|Суть|Цель тренировки")
for fn in os.listdir(SRC):
    if not fn.endswith(".py"):
        continue
    p = os.path.join(SRC, fn)
    with open(p, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if pat.search(line):
                print(f"{fn}:{i}: {line.rstrip()[:160]}")
