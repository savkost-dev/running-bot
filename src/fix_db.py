import sqlite3
conn = sqlite3.connect('../running_bot.db')

# Удаляем старый strava токен у user 2
conn.execute("DELETE FROM user_tokens WHERE user_id=2 AND service='strava'")

# Переносим токен от тестового пользователя
conn.execute("UPDATE user_tokens SET user_id=2 WHERE user_id=1 AND service='strava'")

# Удаляем тестового пользователя
conn.execute("DELETE FROM users WHERE id=1")
conn.execute("DELETE FROM user_preferences WHERE user_id=1")

conn.commit()
conn.close()
print('Готово!')