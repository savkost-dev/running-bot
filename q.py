import sqlite3
con = sqlite3.connect("/opt/running-bot/running_bot.db")
print(con.execute("SELECT id, telegram_id, username FROM users WHERE username=\'t_savkost\'").fetchone())
