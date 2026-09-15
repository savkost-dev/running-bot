"""Перевести всех пользователей на режим ИИ «умный» (smart). Решение Антона 15.09.2026: один боевой режим.

Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/set_all_smart.py
Печатает распределение до/после. Повторный запуск безопасен.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import database as db  # noqa: E402

with db.get_connection() as conn:
    before = conn.execute("SELECT ai_mode, COUNT(*) FROM user_preferences GROUP BY ai_mode").fetchall()
    print("до:", dict(before))
    cur = conn.execute("UPDATE user_preferences SET ai_mode='smart' WHERE ai_mode IS NULL OR ai_mode != 'smart'")
    print("изменено строк:", cur.rowcount)
    after = conn.execute("SELECT ai_mode, COUNT(*) FROM user_preferences GROUP BY ai_mode").fetchall()
    print("после:", dict(after))
