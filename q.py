with open(r"D:\running-bot\src\bot.py", encoding="utf-8") as f:
    t = f.read()
i = t.find("chart_items = [(p, c) for p, c in (")
with open(r"D:\running-bot\q_out.txt", "w", encoding="utf-8") as out:
    out.write(t[i-900:i+1200])
print("ok")
