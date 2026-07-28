"""Где собирается боевой B-промт рекомендации. Запуск: python scripts/probe_b_prompt.py"""
import os, re

SRC = r"D:\running-bot\src"
pat = re.compile(r"СВЯЗЬ ТИПА РАБОТЫ|lowered_by_recovery|recovery_forecast|Подбери группу для бегуна")
for fn in sorted(os.listdir(SRC)):
    if not fn.endswith(".py"):
        continue
    with open(os.path.join(SRC, fn), encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if pat.search(line):
                print(f"{fn}:{i}: {line.rstrip()[:130]}")
