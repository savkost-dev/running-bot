"""Показать строки recommendation_history: кто, дата, тип прогона, режим, группа, размер advice_json.

Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/probe_history.py [YYYY-MM-DD] [run_kind]
Ничего не меняет.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import database as db  # noqa: E402

date = sys.argv[1] if len(sys.argv) > 1 else None
kind = sys.argv[2] if len(sys.argv) > 2 else None
sql = ("SELECT h.user_id, u.name, h.workout_date, h.run_kind, h.workout_type, h.ai_mode, "
       "h.recommended_group, length(h.advice_json), h.saved_at "
       "FROM recommendation_history h LEFT JOIN users u ON u.id = h.user_id WHERE 1=1")
params = []
if date:
    sql += " AND h.workout_date = ?"
    params.append(date)
if kind:
    sql += " AND h.run_kind = ?"
    params.append(kind)
sql += " ORDER BY h.workout_date, h.run_kind, h.user_id"
with db.get_connection() as conn:
    rows = conn.execute(sql, params).fetchall()
print(f"строк: {len(rows)}")
for r in rows:
    print(f"uid={r[0]:<4} {str(r[1])[:20]:<20} {r[2]} {r[3]:<10} {str(r[4]):<8} {str(r[5]):<6} гр={r[6]:<4} json={r[7]:<5} {r[8]}")
