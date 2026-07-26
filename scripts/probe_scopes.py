"""Имена функций и наличие db_user_id вокруг вызовов format_evening_message.
Запуск: python scripts/probe_scopes.py"""
P = r"D:\running-bot\src\bot.py"
with open(P, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
targets = [2634, 2929, 3092]
defs = [(i, l) for i, l in enumerate(lines, 1) if l.startswith("async def") or l.startswith("def ")]
for t in targets:
    d = max((x for x in defs if x[0] < t), key=lambda x: x[0])
    print(f"строка {t} → {d[0]}: {d[1].strip()[:120]}")
    has = any("db_user_id" in lines[j] for j in range(d[0], t))
    print(f"   db_user_id в скоупе до вызова: {has}")
