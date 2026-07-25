"""Миграция VO2max → новые поля (device/manual + приоритет).

Правила:
  - vo2max_source == 'manual' ИЛИ vo2max_locked=1  → vo2max_manual (+priority='manual')
    (замок = «не переписывай моё» — в новой схеме это ручное значение с приоритетом)
  - иначе → vo2max_device (+source из vo2max_source или 'auto'), приоритет не ставим (дефолт device)
  - _at берётся из vo2max_updated_at
  - идемпотентно: строки, где новые поля уже заполнены, пропускаются

Запуск (просмотр): python scripts/migrate_vo2max.py <путь к БД>
Применить:         python scripts/migrate_vo2max.py <путь к БД> --apply
"""
import sqlite3
import sys

if len(sys.argv) < 2:
    print("Укажи путь к БД. Пример: python scripts/migrate_vo2max.py data/running_bot.db")
    sys.exit(1)
DB = sys.argv[1]
APPLY = "--apply" in sys.argv

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT user_id, vo2max, vo2max_source, vo2max_updated_at, vo2max_locked, "
    "vo2max_device, vo2max_manual FROM user_profile WHERE vo2max IS NOT NULL"
).fetchall()

to_manual, to_device, skipped = [], [], 0
for uid, v, src, at, locked, dev, man in rows:
    if dev is not None or man is not None:
        skipped += 1
        continue
    if src == "manual" or locked:
        to_manual.append((v, at, uid))
        print(f"  uid={uid}: {v} → manual (source={src}, locked={locked}, at={at})")
    else:
        to_device.append((v, src or "auto", at, uid))
        print(f"  uid={uid}: {v} → device/{src or 'auto'} (at={at})")

print(f"\nmanual: {len(to_manual)}, device: {len(to_device)}, пропущено (уже мигрировано): {skipped}")
if APPLY:
    conn.executemany(
        "UPDATE user_profile SET vo2max_manual = ?, vo2max_manual_at = ?, "
        "vo2max_priority = 'manual' WHERE user_id = ?", to_manual)
    conn.executemany(
        "UPDATE user_profile SET vo2max_device = ?, vo2max_device_source = ?, "
        "vo2max_device_at = ? WHERE user_id = ?", to_device)
    conn.commit()
    print("Записано.")
elif to_manual or to_device:
    print("Просмотр без записи. Для применения добавь --apply")
conn.close()
