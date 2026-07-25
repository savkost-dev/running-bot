"""Контекст VO2max: автообновление + что уходит в промт.
Запуск: python scripts/probe_vo2ctx.py"""
FILES = [
    (r"D:\running-bot\src\bot.py", [(2985, 3045), (4075, 4200)]),
    (r"D:\running-bot\src\garmin.py", [(505, 565)]),
]
for path, ranges in FILES:
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    for a, b in ranges:
        print(f"───── {path.split(chr(92))[-1]} {a}–{b} ─────")
        for i in range(a, min(b, len(lines)) + 1):
            print(f"{i}: {lines[i - 1].rstrip()}")
