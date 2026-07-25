"""Печать диапазона строк файла. Запуск: python scripts/probe_lines.py <file> <from> <to>"""
import sys

p, a, b = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
with open(p, encoding="utf-8", errors="replace") as f:
    for i, line in enumerate(f, 1):
        if a <= i <= b:
            print(f"{i}: {line.rstrip()}")
        if i > b:
            break
