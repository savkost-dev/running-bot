"""Сигнатура build_charts_stacked. Запуск: python scripts/probe_stacked.py"""
P = r"D:\running-bot\src\ai_package.py"
with open(P, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
start = next(i for i, l in enumerate(lines, 1) if "def build_charts_stacked" in l)
for i in range(start, start + 20):
    print(f"{i}: {lines[i - 1].rstrip()}")
