"""Печать cmd_report и соседних мест построения картинок в bot.py.
Запуск: python scripts/probe_cmd_report.py"""
P = r"D:\running-bot\src\bot.py"
with open(P, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
marks = [i for i, l in enumerate(lines, 1)
         if "def cmd_report" in l or "build_charts" in l or "build_report_card" in l
         or "ai_package" in l]
print("метки:", marks)
start = next((i for i, l in enumerate(lines, 1) if "async def cmd_report" in l), None)
if start:
    end = start + 1
    while end <= len(lines) and not lines[end - 1].startswith("async def cmd_report_user"):
        end += 1
        if end - start > 160:
            break
    for i in range(start, min(end, len(lines) + 1)):
        print(f"{i}: {lines[i - 1].rstrip()}")
