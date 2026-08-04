"""Сравнение: темп группы из задания vs темпы сегментов (что уходит в Garmin).
Пишет читаемую таблицу в tmp/segments.txt (utf-8).
Запуск (на сервере): venv/bin/python3 scripts/probe_segments.py 2026-08-04
Затем забрать: scp root@...:/opt/running-bot/tmp/segments.txt D:\\running-bot\\tmp\\
"""
import json
import os
import sqlite3
import sys

date = sys.argv[1] if len(sys.argv) > 1 else "2026-08-04"
OUT = os.path.join(os.path.dirname(__file__), "..", "tmp", "segments.txt")
os.makedirs(os.path.dirname(OUT), exist_ok=True)

conn = sqlite3.connect("running_bot.db")
row = conn.execute(
    "SELECT analyzed_json FROM workout_analysis WHERE workout_date=? AND is_valid=1 "
    "ORDER BY updated_at DESC LIMIT 1", (date,)).fetchone()
conn.close()
if not row:
    print("анализ не найден")
    sys.exit(1)
d = json.loads(row[0])

lines = [f"Анализ {date}", ""]
for st in d.get("structure") or []:
    segs = ", ".join(f"{s.get('distance_m')}м" for s in (st.get("segments") or []))
    lines.append(f"структура блок {st.get('block')}: {st.get('reps')}× "
                 f"{st.get('work_distance_m')}м [{segs}]")
lines.append("")
lines.append(f"{'группа':>8} | {'темп блока':>10} | сегменты (то, что уходит в часы)")
lines.append("-" * 70)
for g in d.get("groups") or []:
    for b in g.get("blocks") or []:
        blk = f"{b.get('work_pace_start')}→{b.get('work_pace_end')}"
        segs = " | ".join(
            f"{s.get('distance_m')}м {s.get('work_pace_start')}→{s.get('work_pace_end')}"
            for s in (b.get("segments") or [])) or "нет сегментов"
        lines.append(f"{str(g.get('number')):>8} | {blk:>10} | {segs}")

text = "\n".join(lines)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(text + "\n")
print(text)
