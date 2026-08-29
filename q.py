import sqlite3
con = sqlite3.connect("/opt/running-bot/running_bot.db")
rows = con.execute("""SELECT p.user_id, COALESCE(u.username,u.name), p.rec_group, p.ai_mode, p.answer, p.created_at
FROM pace_feedback p JOIN users u ON u.id=p.user_id
WHERE p.workout_date='2026-08-30' ORDER BY p.created_at""").fetchall()
for r in rows:
    print(r)
