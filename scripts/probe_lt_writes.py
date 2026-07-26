"""Точки записи лактатного порога. Запуск: python scripts/probe_lt_writes.py"""
import os, re

SRC = r"D:\running-bot\src"
pat = re.compile(r"lactate_threshold_pace\s*=|lactate_source\s*=|save_lt_")
for fn in sorted(os.listdir(SRC)):
    if not fn.endswith(".py"):
        continue
    with open(os.path.join(SRC, fn), encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if pat.search(line):
                print(f"{fn}:{i}: {line.rstrip()[:150]}")
