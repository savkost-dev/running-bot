"""Где считаются потолки скорости (MSS) и как выглядят. Запуск: python scripts/probe_ceiling.py"""
import re

P = r"D:\running-bot\src\claude_advisor.py"
pat = re.compile(r"speed_ceiling|ceiling|_mss|потолок", re.IGNORECASE)
with open(P, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
hits = [i for i, l in enumerate(lines, 1) if pat.search(l)]
print("строки:", hits[:40])
# печатаем определение функции потолков, если есть
for i in hits:
    if lines[i - 1].lstrip().startswith("def "):
        for j in range(i, min(i + 30, len(lines))):
            print(f"{j}: {lines[j - 1].rstrip()}")
        print("---")
