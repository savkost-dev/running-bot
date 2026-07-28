"""Печать кода блока «СВЯЗЬ ТИПА РАБОТЫ С ЗОНАМИ» в reco-challenger.
Запуск: python scripts/probe_reco_block.py"""
P = r"D:\running-bot\src\claude_advisor.py"
with open(P, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
ch = next((i for i, l in enumerate(lines) if "def " in l and "_reco_challenger" in l), None)
if ch is None:
    print("Функция _reco_challenger не найдена")
    raise SystemExit(1)
print(f"challenger def на строке {ch + 1}")
hit = next((i for i in range(ch, len(lines)) if "СВЯЗЬ ТИПА РАБОТЫ С ЗОНАМИ" in lines[i]), None)
if hit is None:
    print("Блок в challenger не найден")
    raise SystemExit(1)
for i in range(hit - 2, min(hit + 26, len(lines))):
    print(f"{i + 1}: {lines[i].rstrip()}")
