with open(r"D:\running-bot\src\bot.py", encoding="utf-8") as f:
    lines = f.readlines()
with open(r"D:\running-bot\q_out.txt", "w", encoding="utf-8") as out:
    out.writelines(lines[1230:1300])
print("ok")
