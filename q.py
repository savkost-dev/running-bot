with open(r"D:\running-bot\src\bot.py", encoding="utf-8") as f:
    lines = f.readlines()
with open(r"D:\running-bot\q_out.txt", "w", encoding="utf-8") as out:
    out.writelines(lines[6160:6172])
    out.write("=====\n")
    out.writelines(lines[6220:6232])
print("ok")
