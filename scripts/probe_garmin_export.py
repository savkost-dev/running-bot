"""Где и с каким pace вызывается ask_deepseek_garmin (экспорт в Garmin).
Запуск: python scripts/probe_garmin_export.py"""
import os

SRC = r"D:\running-bot\src"
for fn in ("bot.py", "claude_advisor.py"):
    path = os.path.join(SRC, fn)
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    for i, l in enumerate(lines):
        if "ask_deepseek_garmin(" in l and "def ask_deepseek_garmin" not in l:
            a, b = max(0, i - 12), min(len(lines), i + 4)
            print(f"───── {fn} {a + 1}–{b} ─────")
            for j in range(a, b):
                print(f"{j + 1}: {lines[j].rstrip()}")
            print()
