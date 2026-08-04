"""Строки промта Шага 1 про segments/составные отрезки.
Пишет в tmp/seg_prompt.txt (utf-8). Запуск: python scripts/probe_seg_prompt.py"""
import os
import re

P = r"D:\running-bot\src\claude_advisor.py"
OUT = r"D:\running-bot\tmp\seg_prompt.txt"
pat = re.compile(r"segment|work_pace_start|work_pace_end|\u0441\u043e\u0441\u0442\u0430\u0432\u043d", re.IGNORECASE)
with open(P, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
with open(OUT, "w", encoding="utf-8") as out:
    for i, l in enumerate(lines, 1):
        if pat.search(l) and i < 1035:  # только зона промта анализа (до билдеров рекомендации)
            out.write(f"{i}: {l.rstrip()[:180]}\n")
print(f"OK -> {OUT}")
