with open(r"D:\running-bot\src\announce_brief.py", encoding="utf-8") as f:
    t = f.read()
i = t.find("_MODES_PROMPT")
j = t.find("def _modes_from_ai")
with open(r"D:\running-bot\q_out.txt", "w", encoding="utf-8") as out:
    out.write(t[i:j])
print("ok")
