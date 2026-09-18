"""Разведка библиотеки тренировок Garmin Connect у одного пользователя (18.09.2026).

Только ЧТЕНИЕ: ничего не удаляет, в базу не пишет.
Цель — понять, сколько файлов накопилось в библиотеке и какие самые старые.

Запуск на сервере:
    cd /opt/running-bot && venv/bin/python3 scripts/probe_garmin_workouts.py <uid>
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import garmin  # noqa: E402

PAGE = 200


def _fetch(client, start, limit):
    fn = getattr(client, "connectapi", None) or client.garth.connectapi
    return fn(f"/workout-service/workouts?start={start}&limit={limit}") or []


async def main():
    if len(sys.argv) < 2:
        print("Укажи uid: probe_garmin_workouts.py 2")
        return
    uid = int(sys.argv[1])

    client = await garmin._client(uid)
    if not client:
        print(f"uid {uid}: клиент не создан (креды/блок)")
        return

    items = []
    start = 1
    while True:
        chunk = await asyncio.to_thread(_fetch, client, start, PAGE)
        if not chunk:
            break
        items.extend(chunk)
        if len(chunk) < PAGE:
            break
        start += PAGE

    print(f"uid {uid}: всего тренировок в библиотеке — {len(items)}")

    def _created(w):
        return str(w.get("createdDate") or w.get("updateDate") or "")

    dd = [w for w in items if str(w.get("workoutName") or "").upper().startswith("DD")]
    print(f"из них наших (имя с DD): {len(dd)}, чужих: {len(items) - len(dd)}\n")

    print("10 самых старых наших:")
    for w in sorted(dd, key=_created)[:10]:
        print(f"   • {w.get('workoutName')} | создана {_created(w)[:10]} | id={w.get('workoutId')}")

    print("\n5 самых свежих наших:")
    for w in sorted(dd, key=_created, reverse=True)[:5]:
        print(f"   • {w.get('workoutName')} | создана {_created(w)[:10]} | id={w.get('workoutId')}")

    if items:
        print(f"\nвсе поля первой записи: {sorted(items[0].keys())}")


if __name__ == "__main__":
    asyncio.run(main())
