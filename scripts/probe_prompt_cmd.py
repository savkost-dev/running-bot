"""Где живёт last_prompt и какая админ-команда его отдаёт (для расширения).
Пишет в tmp/prompt_cmd_hits.txt. Запуск: python scripts/probe_prompt_cmd.py"""
import os
import re

SRC = r"D:\running-bot\src"
OUT = r"D:\running-bot\tmp\prompt_cmd_hits.txt"
pat = re.compile(r"last_prompt|/prompt|prompt_cmd|Последний промпт", re.IGNORECASE)
with open(OUT, "w", encoding="utf-8") as out:
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(SRC, fn), encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if pat.search(line):
                    out.write(f"{fn}:{i}: {line.rstrip()[:160]}\n")
print(f"OK -> {OUT}")
