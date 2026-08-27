with open(r"D:\running-bot\src\claude_advisor.py", encoding="utf-8") as f:
    lines = f.readlines()
with open(r"D:\running-bot\q_out.txt", "w", encoding="utf-8") as out:
    out.writelines(lines[705:730])
print("ok")
