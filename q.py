import sqlite3, json
con = sqlite3.connect("/opt/running-bot/running_bot.db")
r = con.execute("SELECT analyzed_json FROM workout_analysis WHERE workout_date=? AND is_valid=1 ORDER BY id DESC LIMIT 1", ("2026-09-01",)).fetchone()
a = json.loads(r[0])
print("groups:", [g.get("number") for g in a.get("groups", [])])
print("extra:", a.get("extra_groups"))
