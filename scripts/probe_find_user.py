"""Разведчик: найти пользователя по части имени/ника и показать его подключения.

Печатает id, имя, телеграм-ник и strava_athlete_id из user_tokens
(ник Strava бот не хранит — только числовой id спортсмена).
Запуск на сервере:
    cd /opt/running-bot && venv/bin/python scripts/probe_find_user.py Баглаев
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import get_connection


def main():
    if len(sys.argv) < 2:
        print("usage: probe_find_user.py <часть имени или ника>")
        return
    q = f"%{sys.argv[1]}%"

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT u.id, u.name, u.username, u.telegram_id
            FROM users u
            WHERE u.name LIKE ? OR u.username LIKE ?
            ORDER BY u.id
        """, (q, q)).fetchall()

        if not rows:
            print("НЕ НАЙДЕНО")
            return

        for uid, name, uname, tg_id in rows:
            print(f"id={uid} | {name} | @{uname} | tg={tg_id}")
            toks = conn.execute(
                "SELECT service, strava_athlete_id FROM user_tokens WHERE user_id = ?",
                (uid,)).fetchall()
            for service, athlete_id in toks:
                print(f"   сервис: {service} | strava_athlete_id={athlete_id}")
                if athlete_id:
                    print(f"   https://www.strava.com/athletes/{athlete_id}")
            if not toks:
                print("   подключений нет")


if __name__ == "__main__":
    main()
