"""Печать факторного блока challenger с контекстом. Запуск: python scripts/probe_factor_block.py"""
P = r"D:\running-bot\src\claude_advisor.py"
with open(P, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
hit = next((i for i, l in enumerate(lines, 1) if "ФАКТОРЫ ИНТЕНСИВНОСТИ" in l), None)
if not hit:
    print("Блок не найден — патч ещё не применён")
else:
    for i in range(max(hit - 4, 1), min(hit + 20, len(lines)) + 1):
        print(f"{i}: {lines[i - 1].rstrip()}")
