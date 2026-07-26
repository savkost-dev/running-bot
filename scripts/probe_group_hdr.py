"""Где рендерится заголовок «🎯 Группа …» рекомендации. Запуск: python scripts/probe_group_hdr.py"""
import re

P = r"D:\running-bot\src\claude_advisor.py"
pat = re.compile(r"🎯|Твои данные|reason|recommended_group")
with open(P, encoding="utf-8", errors="replace") as f:
    for i, line in enumerate(f, 1):
        if pat.search(line):
            print(f"{i}: {line.rstrip()[:150]}")
