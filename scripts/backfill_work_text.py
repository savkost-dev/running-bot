"""Бэкфилл: work_text из raw_text -> внутрь analyzed_json (рядом с summary/overall_purpose).

Запуск (сначала всегда просмотр, без записи):
    python scripts/backfill_work_text.py <путь к БД>
Применить:
    python scripts/backfill_work_text.py <путь к БД> --apply

Локальная копия: data/running_bot.db. На сервере: running_bot.db в корне.
Обрабатывает только строки, где в analyzed_json ещё нет непустого work_text.
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from telegram_reader import _extract_work_block  # noqa: E402

if len(sys.argv) < 2:
    print("Укажи путь к БД. Пример: python scripts/backfill_work_text.py data/running_bot.db")
    sys.exit(1)
DB = sys.argv[1]
APPLY = "--apply" in sys.argv

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT id, post_id, workout_date, raw_text, analyzed_json FROM workout_analysis"
).fetchall()

todo, skipped = [], 0
for rid, post_id, wdate, raw, aj in rows:
    try:
        d = json.loads(aj) if aj else None
    except Exception:
        d = None
    if not isinstance(d, dict):
        skipped += 1
        continue
    if (d.get("work_text") or "").strip():
        skipped += 1
        continue
    wt = (_extract_work_block(raw or "") or "").strip()
    if not wt:
        print(f"  ! id={rid} {wdate}: работа из raw_text не извлеклась — пропуск")
        skipped += 1
        continue
    d["work_text"] = wt
    todo.append((json.dumps(d, ensure_ascii=False), rid))
    print(f"  id={rid} {wdate}: work_text = {wt[:90]}{'…' if len(wt) > 90 else ''}")

print(f"\nК обновлению: {len(todo)}, пропущено: {skipped}")
if APPLY and todo:
    conn.executemany("UPDATE workout_analysis SET analyzed_json = ? WHERE id = ?", todo)
    conn.commit()
    print("Записано.")
elif todo:
    print("Просмотр без записи. Для применения добавь --apply")
conn.close()
