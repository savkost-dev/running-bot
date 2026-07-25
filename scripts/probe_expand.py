"""Печать функции _expand_plan_roles из strava.py. Запуск: python scripts/probe_expand.py"""
P = r"D:\running-bot\src\strava.py"
with open(P, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
start = next(i for i, l in enumerate(lines) if "def _expand_plan_roles" in l)
end = start + 1
while end < len(lines) and (not lines[end].startswith("def ") and not lines[end].startswith("async def ")):
    end += 1
for i in range(start, min(end, start + 80)):
    print(f"{i + 1}: {lines[i].rstrip()}")
