"""
backfill_profile.py — разовое заполнение пола и даты рождения в user_profile.

Проходит по всем пользователям, у кого подключены сервисы, тянет gender/birthdate
из сервиса (Garmin/COROS/Polar/Strava) и записывает в user_profile, если там пусто.

Запуск на сервере:
    cd /opt/running-bot/src
    /opt/running-bot/venv/bin/python3 backfill_profile.py          # реальная запись
    /opt/running-bot/venv/bin/python3 backfill_profile.py --dry    # только показать, без записи

Данные статичные — запускается разово. Не трогает прод-флоу.

Приоритет источников:
- gender:    garmin → polar → coros → strava (garmin/polar однозначны; coros sex=0/1 допущение)
- birthdate: garmin → polar → coros (strava ДР не отдаёт)
"""
import asyncio
import sys

import database as db
import garmin
import coros
import polar
import strava

# Порядок сервисов по приоритету для каждого поля
GENDER_PRIORITY    = ["garmin", "polar", "coros", "strava"]
BIRTHDATE_PRIORITY = ["garmin", "polar", "coros"]

_FETCHERS = {
    "garmin": garmin.get_profile,
    "coros":  coros.get_profile,
    "polar":  polar.get_profile,
    "strava": strava.get_profile,
}


async def _collect_profiles(db_user_id: int, services: set[str]) -> dict:
    """Тянет profile из всех подключённых сервисов пользователя."""
    profiles: dict = {}
    for svc in services:
        fetcher = _FETCHERS.get(svc)
        if not fetcher:
            continue
        try:
            p = await fetcher(db_user_id)
            if p:
                profiles[svc] = p
        except Exception as e:
            print(f"  [{svc}] ошибка: {e}")
    return profiles


def _pick(profiles: dict, field: str, priority: list[str]) -> tuple[str | None, str | None]:
    """Берёт значение поля по приоритету сервисов. Возвращает (значение, источник)."""
    for svc in priority:
        p = profiles.get(svc)
        if p and p.get(field):
            return p[field], svc
    return None, None


async def main(dry_run: bool = False):
    # Собираем всех пользователей с любым из сервисов
    all_user_ids: set[int] = set()
    user_services: dict[int, set[str]] = {}
    for svc in _FETCHERS:
        for telegram_id, name, uid in db.get_users_with_service(svc):
            all_user_ids.add(uid)
            user_services.setdefault(uid, set()).add(svc)

    print(f"Пользователей с сервисами: {len(all_user_ids)}")
    print(f"{'РЕЖИМ: dry-run (без записи)' if dry_run else 'РЕЖИМ: запись в БД'}")
    print("=" * 60)

    filled, skipped = 0, 0

    for uid in sorted(all_user_ids):
        services = user_services[uid]
        prof = db.get_user_profile(uid) or {}
        cur_gender = prof.get("gender")
        cur_bdate  = prof.get("birthdate")

        # Если оба уже заполнены — пропускаем
        if cur_gender and cur_bdate:
            skipped += 1
            continue

        profiles = await _collect_profiles(uid, services)
        if not profiles:
            print(f"user {uid}: сервисы {services} — данных нет")
            continue

        new_gender, g_src = _pick(profiles, "gender", GENDER_PRIORITY)
        new_bdate,  b_src = _pick(profiles, "birthdate", BIRTHDATE_PRIORITY)

        # Не перезаписываем уже заполненное
        write_gender = new_gender if not cur_gender else None
        write_bdate  = new_bdate  if not cur_bdate  else None

        if not write_gender and not write_bdate:
            print(f"user {uid}: нечего добавить (профили: {list(profiles)})")
            continue

        parts = []
        if write_gender:
            parts.append(f"gender={write_gender} [{g_src}]")
        if write_bdate:
            parts.append(f"birthdate={write_bdate} [{b_src}]")
        print(f"user {uid}: {', '.join(parts)}")

        if not dry_run:
            db.save_user_profile(
                uid,
                gender=write_gender,
                birthdate=write_bdate,
            )
        filled += 1

    print("=" * 60)
    print(f"Заполнено: {filled}, пропущено (уже есть): {skipped}")


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    asyncio.run(main(dry_run=dry))
