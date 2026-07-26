"""Миграция лактатного порога → новые поля (device/manual + приоритет).

Правила (зеркало migrate_vo2max):
  - lactate_source == 'manual' ИЛИ lactate_locked=1 → lt_pace_manual/lt_hr_manual (+lt_priority='manual')
  - иначе → lt_pace_device/lt_hr_device (+source из lactate_source или 'auto')
  - _at из lactate_updated_at (если колонки нет — updated_at, иначе пусто)
  - идемпотентно: строки с уже заполненными новыми полями пропускаются

Запуск (просмотр): python scripts/migrate_lt.py <путь к БД>
Применить:         python scripts/migrate_lt.py <путь к БД> --apply
"""
import sqlite3
import sys

if len(sys.argv) < 2:
    print("Укажи путь к БД. Пример: python scripts/migrate_lt.py data/running_bot.db")
    sys.exit(1)
DB = sys.argv[1]
APPLY = "--apply" in sys.argv

conn = sqlite3.connect(DB)
cols = {r[1] for r in conn.execute("PRAGMA table_info(user_profile)")}
at_col = ("lactate_updated_at" if "lactate_updated_at" in cols
          else ("updated_at" if "updated_at" in cols else None))
at_expr = at_col if at_col else "NULL"

rows = conn.execute(
    f"SELECT user_id, lactate_threshold_pace, lactate_threshold_hr, lactate_source, "
    f"lactate_locked, {at_expr}, lt_pace_device, lt_pace_manual "
    f"FROM user_profile WHERE lactate_threshold_pace IS NOT NULL"
).fetchall()

to_manual, to_device, skipped = [], [], 0
for uid, pace, hr, src, locked, at, dev, man in rows:
    if dev is not None or man is not None:
        skipped += 1
        continue
    if src == "manual" or locked:
        to_manual.append((pace, hr, at, uid))
        print(f"  uid={uid}: {pace}@{hr} → manual (source={src}, locked={locked}, at={at})")
    else:
        to_device.append((pace, hr, src or "auto", at, uid))
        print(f"  uid={uid}: {pace}@{hr} → device/{src or 'auto'} (at={at})")

print(f"\nmanual: {len(to_manual)}, device: {len(to_device)}, пропущено: {skipped}")
print(f"колонка даты: {at_col or 'нет'}")
if APPLY:
    conn.executemany(
        "UPDATE user_profile SET lt_pace_manual = ?, lt_hr_manual = ?, "
        "lt_manual_at = ?, lt_priority = 'manual' WHERE user_id = ?", to_manual)
    conn.executemany(
        "UPDATE user_profile SET lt_pace_device = ?, lt_hr_device = ?, "
        "lt_device_source = ?, lt_device_at = ? WHERE user_id = ?", to_device)
    conn.commit()
    print("Записано.")
elif to_manual or to_device:
    print("Просмотр без записи. Для применения добавь --apply")
conn.close()
