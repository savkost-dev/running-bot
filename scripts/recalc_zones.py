"""Массовый пересчёт темповых зон после смены точки отсчёта (этап 2, 05.08.2026).

Показывает по каждому пользователю старые и новые зоны, с --apply сохраняет.
Зоны иначе обновились бы только при изменении профиля или в ночном джобе.

Запуск (просмотр): venv/bin/python3 scripts/recalc_zones.py
Применить:         venv/bin/python3 scripts/recalc_zones.py --apply
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import database as db  # noqa: E402
import zones as z  # noqa: E402

APPLY = "--apply" in sys.argv

with db.get_connection() as conn:
    uids = [r[0] for r in conn.execute(
        "SELECT DISTINCT user_id FROM user_profile ORDER BY user_id").fetchall()]

print(f"пользователей с профилем: {len(uids)}\n")
changed = skipped = 0
for uid in uids:
    profile = db.get_user_profile(uid)
    cache = db.get_athlete_cache(uid)
    old = (db.get_pace_zones_raw(uid) or {}).get("zones") or {}
    new = z.calculate_pace_zones(profile, cache)
    if not new:
        print(f"uid={uid:3d}: нет данных для расчёта — пропуск")
        skipped += 1
        continue
    nz = new["zones"]
    mark = "=" if old.get("threshold") == nz.get("threshold") else "→"
    print(f"uid={uid:3d} [{new['source']:>13}] VDOT {new['vdot']:.1f} | "
          f"ПАНО {old.get('threshold', '—')} {mark} {nz.get('threshold')} | "
          f"МПК {old.get('interval', '—')} {mark} {nz.get('interval')} | "
          f"R {old.get('repetition', '—')} {mark} {nz.get('repetition')}")
    if APPLY:
        db.save_pace_zones(uid, nz, new["source"])
        changed += 1

print(f"\nитого: пересчитано={changed}, без данных={skipped}")
if not APPLY:
    print("Просмотр без записи. Для применения добавь --apply")
