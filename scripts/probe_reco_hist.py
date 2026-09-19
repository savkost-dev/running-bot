"""Разведка: последние записи recommendation_history у uid 2 (формат даты, run_kind, groups_pct). Локальная копия базы."""
import sqlite3
con = sqlite3.connect("D:/running-bot/data/running_bot.db")
for r in con.execute("SELECT workout_date, run_kind, recommended_group, ai_mode, groups_pct FROM recommendation_history WHERE user_id=2 ORDER BY saved_at DESC LIMIT 3"):
    print(r)
