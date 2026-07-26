"""Контекст четырёх точек записи ЛП в bot.py. Запуск: python scripts/probe_lt_ctx.py"""
P = r"D:\running-bot\src\bot.py"
RANGES = [(1705, 1732), (2225, 2262), (2300, 2320), (4190, 4212)]
with open(P, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
for a, b in RANGES:
    print(f"───── {a}–{b} ─────")
    for i in range(a, min(b, len(lines)) + 1):
        print(f"{i}: {lines[i - 1].rstrip()}")
