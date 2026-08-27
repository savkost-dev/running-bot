with open(r"D:\running-bot\src\claude_advisor.py", encoding="utf-8") as f:
    lines = f.readlines()
with open(r"D:\running-bot\q_out.txt", "w", encoding="utf-8") as out:
    out.writelines(lines[1208:1245])
    out.write("=====\n")
    out.writelines(lines[1425:1462])
print("ok")
