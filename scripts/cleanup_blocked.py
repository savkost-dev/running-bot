"""Разовая чистка за заблокировавшими бота (22.08.2026).

Без --apply только ПОКАЗЫВАЕТ: кто заблокировал бота и какие внешние
подключения за ним остались (Strava / Polar).

С --apply по каждому: отзывает доступ НА СТОРОНЕ сервиса (у Strava это
освобождает слот в лимите приложения), и только при успехе удаляет
сохранённый ключ у нас. Профиль, зоны и история не трогаются.
"""
import sys
import asyncio

sys.path.insert(0, "/opt/running-bot/src")

from database import get_connection, get_token, delete_token  # noqa: E402
import strava  # noqa: E402
import polar   # noqa: E402

SERVICES = ("strava", "polar")
APPLY = "--apply" in sys.argv
ALL = "--all" in sys.argv


def all_with_tokens() -> list:
    """[(uid, кто, [сервисы], активен), ...] по ВСЕМ, у кого есть доступ."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT u.id,
                   COALESCE(u.username, u.name, CAST(u.telegram_id AS TEXT)),
                   p.is_active
            FROM users u
            LEFT JOIN user_preferences p ON p.user_id = u.id
            ORDER BY u.id
        """).fetchall()

    out = []
    for uid, who, active in rows:
        svcs = [s for s in SERVICES if get_token(uid, s)]
        if svcs:
            out.append((uid, who, svcs, active))
    return out


def blocked_with_tokens() -> list:
    """[(uid, telegram_id, кто, [сервисы]), ...] по неактивным с подключениями."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT u.id, u.telegram_id,
                   COALESCE(u.username, u.name, CAST(u.telegram_id AS TEXT)),
                   p.deactivated_at
            FROM users u
            JOIN user_preferences p ON p.user_id = u.id
            WHERE p.is_active = 0
            ORDER BY u.id
        """).fetchall()

    out = []
    for uid, tid, who, since in rows:
        svcs = [s for s in SERVICES if get_token(uid, s)]
        if svcs:
            out.append((uid, tid, who, svcs, (since or "")[:10]))
    return out


async def revoke(uid: int, service: str) -> bool:
    if service == "strava":
        return await strava.deauthorize(uid)
    if service == "polar":
        return await polar.deregister(uid)
    return False


async def main() -> None:
    uid_args = [a for a in sys.argv[1:] if a.isdigit()]
    if uid_args:
        with get_connection() as conn:
            for a in uid_args:
                row = conn.execute(
                    "SELECT id, telegram_id, name, username, created_at "
                    "FROM users WHERE id = ?", (int(a),)).fetchone()
                if not row:
                    print(f"uid={a}: такого нет в базе")
                    continue
                _id, tid, name, uname, created = row
                print(f"uid={_id} telegram_id={tid} {name or '—'} "
                      f"@{uname or '—'} с {(created or '')[:10]}")
        return

    if ALL:
        rows = all_with_tokens()
        n_str = sum(1 for _u, _w, s, _a in rows if "strava" in s)
        n_pol = sum(1 for _u, _w, s, _a in rows if "polar" in s)
        print(f"Всего с доступом: Strava {n_str}, Polar {n_pol}\n")
        for uid, who, svcs, active in rows:
            state = "активен" if active == 1 else (
                "ЗАБЛОКИРОВАЛ" if active == 0 else "нет настроек")
            print(f"  uid={uid:<5} @{who:<24} {', '.join(svcs):<14} {state}")
        return

    items = blocked_with_tokens()
    if not items:
        print("Заблокировавших бота с подключениями Strava/Polar нет.")
        return

    print(f"Заблокировали бота и подключения остались: {len(items)}\n")
    for uid, _tid, who, svcs, since in items:
        print(f"  uid={uid:<5} @{who:<24} {', '.join(svcs):<14} с {since or '—'}")

    if not APPLY:
        print("\nЭто только показ, ничего не изменено.")
        print("Чтобы отозвать доступ, запустить этот же скрипт с --apply")
        return

    print("\nОтзываю доступ на стороне сервисов...")
    ok_n = fail_n = 0
    for uid, _tid, who, svcs, _since in items:
        for s in svcs:
            try:
                ok = await revoke(uid, s)
            except Exception as e:
                ok = False
                print(f"  @{who} {s}: ошибка {str(e)[:80]}")
            if ok:
                delete_token(uid, s)
                ok_n += 1
                print(f"  @{who} {s}: отозвано у сервиса, ключ удалён")
            else:
                fail_n += 1
                print(f"  @{who} {s}: НЕ УДАЛОСЬ — ключ оставлен, разбираться вручную")

    print(f"\nИтого: успешно {ok_n}, не вышло {fail_n}")


if __name__ == "__main__":
    asyncio.run(main())
