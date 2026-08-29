with open(r"D:\running-bot\src\bot.py", encoding="utf-8") as f:
    t = f.read()
i = t.find("reco_line = ")
with open(r"D:\running-bot\q_out.txt", "w", encoding="utf-8") as out:
    out.write(t[i-200:i+1900])
print("ok")
