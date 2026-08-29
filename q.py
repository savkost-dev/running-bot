with open(r"D:\running-bot\deploy\www\index.html", encoding="utf-8") as f:
    t = f.read()
i = t.find("ример рекомендации")
print("idx:", i)
with open(r"D:\running-bot\q_out.txt", "w", encoding="utf-8") as out:
    out.write(t[max(0, i-500):i+2800])
print("ok")
