"""Распределение режима ИИ (ai_mode из user_preferences) среди пользователей с рассчитанными зонами.

Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/probe_ai_modes.py
Ничего не меняет.
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import database as db  # noqa: E402

users = [(tid, name) for tid, name, _un, has in db.get_all_users_with_status() if has]
cnt, names = Counter(), {}
with db.get_connection() as conn:
    for tid, name in users:
        row = conn.execute(
            "SELECT p.ai_mode FROM users u LEFT JOIN user_preferences p ON p.user_id = u.id "
            "WHERE u.telegram_id = ?", (tid,)).fetchone()
        mode = (row[0] if row and row[0] else "smart(default)")
        cnt[mode] += 1
        names.setdefault(mode, []).append(name)
print(f"с зонами: {len(users)}")
for mode, n in cnt.most_common():
    print(f"  {mode:16} {n:3}   " + ", ".join(names[mode][:12]) + (" …" if n > 12 else ""))
