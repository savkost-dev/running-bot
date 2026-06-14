"""Проверка таблицы mornings — read-only.

Печатает: число строк в mornings и первые 20 записей (user_id, date, sources,
TR, BB, HRV, RHR, сон, подъём). Нужен, чтобы убедиться, что таблица создана
и разовое копирование из unified_cache отработало после деплоя.

Пишет в базу: НЕТ (read-only).
Импортирует: только sqlite3 + путь к running_bot.db. НЕ импортирует bot.py.

Запуск на сервере:
    /opt/running-bot/venv/bin/python3 scripts/check_mornings.py
"""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "running_bot.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        # Существует ли таблица
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mornings'"
        ).fetchone()
        if not exists:
            print("Таблица mornings НЕ найдена.")
            return

        total = conn.execute("SELECT COUNT(*) FROM mornings").fetchone()[0]
        print(f"mornings: строк = {total}")

        rows = conn.execute(
            "SELECT user_id, date, sources, morning_tr, morning_bb, morning_hrv, "
            "morning_rhr, morning_sleep_h, morning_wake_at "
            "FROM mornings ORDER BY user_id, date LIMIT 20"
        ).fetchall()
        if not rows:
            print("(записей нет)")
            return
        print(f"{'uid':>4} {'date':<11} {'sources':<22} {'TR':>4} {'BB':>4} "
              f"{'HRV':>5} {'RHR':>4} {'сон':>4} подъём")
        for r in rows:
            uid, date, sources, tr, bb, hrv, rhr, sleep_h, wake = r
            wake_s = (wake or "")[11:16] if wake else "—"
            print(f"{uid:>4} {str(date):<11} {str(sources or '—'):<22} "
                  f"{str(tr if tr is not None else '—'):>4} "
                  f"{str(bb if bb is not None else '—'):>4} "
                  f"{str(hrv if hrv is not None else '—'):>5} "
                  f"{str(rhr if rhr is not None else '—'):>4} "
                  f"{str(sleep_h if sleep_h is not None else '—'):>4} {wake_s}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
