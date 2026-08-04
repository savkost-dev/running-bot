"""Печать блока промта Шага 1 про составные отрезки (строки 676-700).
Пишет в tmp/seg_block.txt (utf-8). Запуск: python scripts/probe_seg_block.py"""
P = r"D:\running-bot\src\claude_advisor.py"
OUT = r"D:\running-bot\tmp\seg_block.txt"
with open(P, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
with open(OUT, "w", encoding="utf-8") as out:
    for i in range(676, 701):
        out.write(f"{i}: {lines[i - 1].rstrip()}\n")
print(f"OK -> {OUT}")
