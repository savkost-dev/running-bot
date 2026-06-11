"""Перегон нормализации (run_normalization) для пользователя — вручную.

Что делает: читает уже сохранённое сырьё (raw_service_data) и пересобирает
unified_cache.unified_json по текущему коду нормализатора. Сырьё НЕ перезабирает,
morning_* НЕ трогает.

Пишет в базу: unified_cache (unified_json, sources, updated_at).
Импортирует: data_normalizer, database. НЕ импортирует bot.py.

Запуск:
    venv/bin/python3 scripts/renormalize.py 6        # один юзер (Ксения)
    venv/bin/python3 scripts/renormalize.py all      # все юзеры
"""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import database as db
from data_normalizer import run_normalization


def _show_tr(uid: int):
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT unified_json FROM unified_cache WHERE user_id=?", (uid,)
        ).fetchone()
    if row and row[0]:
        u = json.loads(row[0])
        tr = u.get("s3_training_readiness") or {}
        print(f"  user={uid}: TR={tr.get('score')} ({tr.get('level')}) "
              f"rec_daily={u.get('s3_recovery_daily')} tsb={u.get('s3_recovery_total')}")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if not arg:
        print("Укажи db_user_id или all")
        return
    if arg == "all":
        uids = [u["db_user_id"] for u in db.get_users_list_for_b()]
    else:
        uids = [int(arg)]
    for uid in uids:
        try:
            run_normalization(uid)
            _show_tr(uid)
        except Exception as e:
            print(f"  user={uid}: ошибка {e}")


if __name__ == "__main__":
    main()
