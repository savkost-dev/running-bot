"""Все вызовы format_evening_message. Запуск: python scripts/probe_fmt_calls.py"""
import os, re

SRC = r"D:\running-bot\src"
for fn in sorted(os.listdir(SRC)):
    if not fn.endswith(".py"):
        continue
    with open(os.path.join(SRC, fn), encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if "format_evening_message(" in line:
                print(f"{fn}:{i}: {line.rstrip()[:160]}")
