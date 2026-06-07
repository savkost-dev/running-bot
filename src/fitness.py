"""Данные физической формы (fitness) и кэш атлета.

Вынесено из bot.py без изменения логики. Чистый лист: тянет только из
database/сервисов/strava, обратных импортов в bot.py не имеет.

Функции:
- refresh_athlete_cache      — обновляет кэш CTL/ATL/TSB, прогнозы, соревнования (Strava)
- get_fitness_data           — fitness из Strava (быстрые + кэш медленных)
- get_garmin_fitness_data    — fitness из Garmin Connect
- get_coros_fitness_data     — fitness из COROS
- get_polar_fitness_data     — fitness из Polar
- _get_vo2max_from_tracker   — VO2max из первого доступного трекера
"""
import asyncio
import logging

from database import get_token, save_athlete_cache, get_athlete_cache
from strava import get_full_athlete_data

logger = logging.getLogger(__name__)


# ── КЭШ АТЛЕТА ───────────────────────────────────────────────

async def refresh_athlete_cache(db_user_id: int, access_token: str, notify_msg=None) -> dict | None:
    """
    Обновляет кэш данных атлета (CTL/ATL/TSB, прогнозы, соревнования).
    Занимает 30-60 сек — вызывать только при подключении или по запросу.
    """
    try:
        if notify_msg:
            await notify_msg.edit_text(
                "⏳ Загружаю данные из Strava...\n"
                "Это займёт около минуты (только первый раз)"
            )

        athlete_data = await get_full_athlete_data(access_token)

        save_athlete_cache(
            db_user_id,
            athlete_data["training_load"],
            athlete_data["predictions"],
            athlete_data["last_race"]
        )

        logger.info(f"Кэш атлета обновлён для user_id={db_user_id}")
        return athlete_data

    except Exception as e:
        logger.error(f"Ошибка обновления кэша для user_id={db_user_id}: {e}")
        return None


async def get_fitness_data(db_user_id: int, access_token: str) -> dict | None:
    """
    Получает данные атлета:
    - Быстрые (всегда свежие): пробежки за 14 дней + острая нагрузка за 48 ч
    - Медленные (из кэша): CTL/ATL/TSB, прогнозы Риегеля, последнее соревнование
    """
    from strava import get_recent_runs, analyze_fitness, get_recent_48h_load

    # ── Быстрые данные (2 запроса к Strava API) ──────────────
    try:
        runs, load_48h = await asyncio.gather(
            get_recent_runs(access_token, days=14),
            get_recent_48h_load(access_token),
        )
        fitness = analyze_fitness(runs)
        fitness["load_48h"] = load_48h
    except Exception as e:
        logger.error(f"Ошибка получения пробежек: {e}")
        fitness = {"summary": "Нет данных", "total_km": 0, "run_count": 0,
                   "avg_pace": "—", "avg_hr": None, "fatigue_level": "unknown",
                   "load_48h": None}

    # ── Медленные данные (из кэша) ────────────────────────────
    cache = get_athlete_cache(db_user_id)
    if cache:
        fitness["training_load"] = cache["training_load"]
        fitness["predictions"]   = cache["predictions"]
        fitness["last_race"]     = cache["last_race"]
    else:
        logger.info(f"Кэш отсутствует для user_id={db_user_id}, обновляю...")
        athlete_data = await refresh_athlete_cache(db_user_id, access_token)
        if athlete_data:
            fitness["training_load"] = athlete_data["training_load"]
            fitness["predictions"]   = athlete_data["predictions"]
            fitness["last_race"]     = athlete_data["last_race"]

    return fitness


async def get_garmin_fitness_data(db_user_id: int) -> dict | None:
    """
    Получает данные атлета из Garmin Connect — аналог get_fitness_data() для Strava.
    Возвращает dict, совместимый со структурой fitness для промта.
    """
    import garmin as _garmin
    if not get_token(db_user_id, "garmin"):
        return None

    try:
        results = await asyncio.gather(
            _garmin.get_training_load(db_user_id),
            _garmin.get_activities_48h(db_user_id),
            _garmin.get_last_race(db_user_id),
            _garmin.get_best_efforts(db_user_id),
            return_exceptions=True,
        )
    except Exception as e:
        logger.error(f"Garmin fitness data error for {db_user_id}: {e}")
        return None

    training_load, activities_48h, last_race, best_efforts = results

    fitness = {
        "source": "garmin",
        "summary": "",
        "total_km": 0,
        "run_count": 0,
        "avg_pace": "—",
        "avg_hr": None,
        "fatigue_level": "unknown",
    }

    if not isinstance(training_load, Exception) and training_load:
        fitness["training_load"] = training_load
        tsb = training_load.get("tsb")
        if tsb is not None:
            fitness["fatigue_level"] = "fresh" if tsb > 5 else ("tired" if tsb < -15 else "normal")

    if not isinstance(activities_48h, Exception) and activities_48h:
        fitness["load_48h"] = activities_48h
        fitness["total_km"] = activities_48h.get("km_48h", 0)
        fitness["run_count"] = activities_48h.get("sessions_48h", 0)

    if not isinstance(last_race, Exception) and last_race:
        fitness["last_race"] = last_race

    if not isinstance(best_efforts, Exception) and best_efforts:
        fitness["predictions"] = best_efforts

    return fitness


async def get_coros_fitness_data(db_user_id: int) -> dict | None:
    """
    Получает данные атлета из COROS — аналог get_garmin_fitness_data().
    Возвращает dict, совместимый со структурой fitness для промта.
    """
    import coros as _coros
    if not get_token(db_user_id, "coros"):
        return None
    try:
        return await _coros.get_full_data(db_user_id)
    except Exception as e:
        logger.error(f"COROS fitness data error for {db_user_id}: {e}")
        return None


async def get_polar_fitness_data(db_user_id: int) -> dict | None:
    """
    Получает данные атлета из Polar — аналог get_coros_fitness_data().
    Возвращает dict, совместимый со структурой fitness для промта.
    """
    import polar as _polar
    if not get_token(db_user_id, "polar"):
        return None
    try:
        return await _polar.get_full_data(db_user_id)
    except Exception as e:
        logger.error(f"Polar fitness data error for {db_user_id}: {e}")
        return None


async def _get_vo2max_from_tracker(db_user_id: int) -> tuple:
    """Получает VO2max из первого доступного трекера (Garmin → COROS → Polar).
    Возвращает (vo2max: float, tracker_key: str, tracker_name: str) или (None, None, None).
    """
    if get_token(db_user_id, "garmin"):
        try:
            from garmin import get_vo2max as _garmin_vo2max
            val = await _garmin_vo2max(db_user_id)
            if val is not None:
                return float(val), "garmin", "Garmin"
        except Exception as e:
            logger.warning(f"VO2max Garmin fetch error for uid={db_user_id}: {e}")

    if get_token(db_user_id, "coros"):
        try:
            import coros as _coros
            val = await _coros.get_vo2max(db_user_id)
            if val is not None:
                return float(val), "coros", "COROS"
        except Exception as e:
            logger.warning(f"VO2max COROS fetch error for uid={db_user_id}: {e}")

    if get_token(db_user_id, "polar"):
        try:
            import polar as _polar
            val = await _polar.get_vo2max(db_user_id)
            if val is not None:
                return float(val), "polar", "Polar"
        except Exception as e:
            logger.warning(f"VO2max Polar fetch error for uid={db_user_id}: {e}")

    return None, None, None
