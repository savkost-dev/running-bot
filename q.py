import sqlite3, json
con = sqlite3.connect("/opt/running-bot/running_bot.db")
r = con.execute("SELECT id, analyzed_json FROM workout_analysis WHERE workout_date=\'2026-08-25\' ORDER BY id DESC LIMIT 1").fetchone()
a = json.loads(r[1])
print(r[0], json.dumps(a.get("structure"), ensure_ascii=False))
g = next((g for g in a.get("groups", []) if str(g.get("number"))=="3.5"), {})
print("g3.5:", json.dumps(g.get("blocks"), ensure_ascii=False))
