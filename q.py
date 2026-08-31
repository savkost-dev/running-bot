with open(r"D:\running-bot\src\bot.py", encoding="utf-8") as f:
    t = f.read()
i = t.find("async def cmd_activity")
with open(r"D:\running-bot\q_out.txt", "w", encoding="utf-8") as out:
    out.write(t[i:i+3000])
print("ok")
