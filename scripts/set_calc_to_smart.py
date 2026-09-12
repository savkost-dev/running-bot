"""Перевести пользователей с расчётным режимом (calc) на умный (smart). Решение Антона 12.09.2026.

Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/set_calc_to_smart.py
Печатает, сколько строк изменено. Повторный запуск безопасен (0 изменений).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import database as db  # noqa: E402

with db.get_connection() as conn:
    cur = conn.execute("UPDATE user_preferences SET ai_mode='smart' WHERE ai_mode='calc'")
    print("изменено строк:", cur.rowcount)
