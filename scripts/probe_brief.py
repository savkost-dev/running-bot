"""Тест брифа режимов (Шаг 1.5) без Telegram: рендер по последнему валидному анализу.
Запуск (на сервере): venv/bin/python3 scripts/probe_brief.py [YYYY-MM-DD]
Печатает готовый текст брифа. ИИ-вызов реальный (режим deep), кэширует modes."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import announce_brief  # noqa: E402
from database import get_connection  # noqa: E402

date = sys.argv[1] if len(sys.argv) > 1 else None
q = ("SELECT post_id, analyzed_json FROM workout_analysis WHERE is_valid = 1 ")
args = ()
if date:
    q += "AND workout_date = ? "
    args = (date,)
q += "ORDER BY updated_at DESC LIMIT 1"
with get_connection() as conn:
    row = conn.execute(q, args).fetchone()
if not row:
    print("Анализ не найден")
    sys.exit(1)
post_id, aj = row
result = json.loads(aj)
print(f"post_id={post_id}, дата={result.get('workout_date')}\n")
brief = announce_brief.build_admin_brief(result, post_id, "deep")
print(brief or "(бриф не собрался)")
