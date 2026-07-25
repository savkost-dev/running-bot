"""Печать нескольких диапазонов bot.py. Запуск: python scripts/probe_lines2.py"""
P = r"D:\running-bot\src\bot.py"
RANGES = [(1240, 1290), (1415, 1460), (3545, 3620)]
with open(P, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
for a, b in RANGES:
    print(f"───── {a}–{b} ─────")
    for i in range(a, min(b, len(lines)) + 1):
        print(f"{i}: {lines[i - 1].rstrip()}")
