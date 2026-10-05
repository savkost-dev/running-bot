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
- get_last_run / idle_days / get_idle — последняя пробежка, дни простоя, сдвиг зон (02–05.10.2026)
"""
import asyncio
import logging
from datetime import date

from database import get_token, save_athlete_cache, get_athlete_cache, get_strava_activities
from strava import get_full_athlete_data

logger = logging.getLogger(__name__)


# ── ПОСЛЕДНЯЯ ПРОБЕЖКА И ПРОСТОЙ (02.10.2026, Антон) ──────────
# Зачем: рекомендация строится от VO2max и порога, которые за несколько дней не меняются,
# а работа после 3–4 дней полного отдыха идёт заметно хуже («двигатель на холодную»:
# падает объём плазмы, обмен уходит в углеводы). Первый из трёх уровней поправок
# (простой → большая нагрузка → опорно-двигательный), остальные два — отдельными шагами.
#
# Откуда дата: из сырья ночной загрузки каждого трекера (raw_service_data), где теперь лежит
# список тренировок за LAST_RUN_WINDOW_DAYS — Garmin activities_14d, COROS querySportRecords,
# Polar exercises; разбирают его модули трекеров (garmin.last_run_date_from_activities,
# coros_mcp.last_run_date_from_records, polar.last_run_date_from_exercises). Strava — из окна
# strava_activities (strava.last_run_date_from_window). Отдельного хранения дат нет,
# запросов к сервисам при чтении нет. get_last_run берёт самую свежую по всем источникам:
# пробежка, записанная только на одних часах, не должна давать ложный простой.

LAST_RUN_WINDOW_DAYS = 14   # окно списка тренировок у трекеров
RUN_MIN_M = 3000            # пробежка — от 3 км или от 20 минут (раскатка да, прогулка нет)
RUN_MIN_S = 1200
MAX_IDLE_DAYS = 30          # старше — «очень давно», не ошибка


def is_run(dist_m, dur_s) -> bool:
    """Запись тянет на пробежку: от RUN_MIN_M метров или от RUN_MIN_S секунд."""
    try:
        d = float(dist_m or 0)
    except (TypeError, ValueError):
        d = 0.0
    try:
        t = float(dur_s or 0)
    except (TypeError, ValueError):
        t = 0.0
    return d >= RUN_MIN_M or t >= RUN_MIN_S


def latest_date(dates) -> str | None:
    """Самая свежая 'YYYY-MM-DD' из списка (пустые пропускаются) или None."""
    dates = [d for d in dates if d]
    return max(dates) if dates else None


RAW_FRESH_DAYS = 3          # сырьё старше — подключение считаем мёртвым, источник не учитываем


def _raw_of(db_user_id: int, service: str) -> dict | None:
    """Сырьё ночной загрузки сервиса из raw_service_data как dict, или None.
    05.10.2026: только если подключение живое (есть токен) и сырьё свежее RAW_FRESH_DAYS —
    иначе у отключившихся и заблокировавших бота старое сырьё давало ложный простой «30 дней»."""
    import json
    from datetime import datetime, timedelta
    from database import get_raw_service_data
    if not get_token(db_user_id, service):
        return None
    row = get_raw_service_data(db_user_id, service)
    if not row or not row.get("raw_json"):
        return None
    try:
        fetched = datetime.strptime(str(row.get("fetched_at"))[:19], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() - fetched > timedelta(days=RAW_FRESH_DAYS):
            return None
    except (TypeError, ValueError):
        return None
    try:
        raw = json.loads(row["raw_json"])
    except (TypeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def get_last_run(db_user_id: int) -> dict | None:
    """Самая свежая пробежка по всем живым источникам:
    {"date": 'YYYY-MM-DD' | None, "source": str | None, "all": {источник: дата}, "empty": [источники]}.
    "empty" — живые источники со свежим списком тренировок, в котором нет ни одной пробежки
    за LAST_RUN_WINDOW_DAYS. date=None при непустом "empty" значит «не бегал всё окно».
    None — ни один живой источник ничего не знает (фактор простоя выключен).
    Читает только базу: сырьё трекеров и окно Strava."""
    import garmin as _g
    import coros_mcp as _cm
    import polar as _p
    from strava import last_run_date_from_window
    best, empty = {}, []
    raw = _raw_of(db_user_id, "garmin")
    if raw:
        acts = raw.get("activities_14d")
        full = isinstance(acts, list)
        if not full:
            acts = raw.get("activities_48h")   # сырьё до 0.35.0 — только 48 часов
        d = _g.last_run_date_from_activities(acts)
        if d:
            best["garmin"] = d
        elif full:
            empty.append("garmin")
    raw = _raw_of(db_user_id, _cm.SERVICE)
    if raw and raw.get("querySportRecords"):
        d = _cm.last_run_date_from_records(_cm.parse_sport_records(raw["querySportRecords"]))
        if d:
            best["coros_mcp"] = d
        else:
            empty.append("coros_mcp")
    raw = _raw_of(db_user_id, "polar")
    if raw and isinstance(raw.get("exercises"), list):
        d = _p.last_run_date_from_exercises(raw["exercises"])
        if d:
            best["polar"] = d
        else:
            empty.append("polar")
    if get_token(db_user_id, "strava"):
        d = last_run_date_from_window(db_user_id)
        if d:
            best["strava"] = d
    if not best and not empty:
        return None
    src = max(best, key=lambda k: best[k]) if best else None
    return {"date": best[src] if src else None, "source": src, "all": best, "empty": empty}


def get_idle(db_user_id: int, workout_date: str | None) -> dict | None:
    """05.10.2026: простой перед тренировкой — {"days", "shift", "last_run", "source"}.
    days — полные дни без бега до workout_date (нет пробежек во всём окне → LAST_RUN_WINDOW_DAYS),
    shift — сдвиг зон в с/км по zones.idle_shift_sec (0 — правило не действует).
    None — данных нет, правило выключено."""
    import zones as _z
    lr = get_last_run(db_user_id)
    if not lr:
        return None
    days = idle_days(lr["date"], workout_date) if lr["date"] else LAST_RUN_WINDOW_DAYS
    if days is None:
        return None
    return {"days": days, "shift": _z.idle_shift_sec(days), "last_run": lr["date"], "source": lr["source"]}


def idle_days(last_run_date: str | None, workout_date: str | None) -> int | None:
    """Полные дни без бега между последней пробежкой и днём работы.
    Последняя в воскресенье, работа в пятницу → пн, вт, ср, чт = 4.
    Без даты пробежки — None (фактор выключен); старше MAX_IDLE_DAYS — MAX_IDLE_DAYS."""
    if not last_run_date:
        return None
    try:
        lr = date.fromisoformat(str(last_run_date)[:10])
        wd = date.fromisoformat(str(workout_date)[:10]) if workout_date else date.today()
    except ValueError:
        return None
    n = (wd - lr).days - 1
    if n < 0:
        return 0
    return min(n, MAX_IDLE_DAYS)


# ── КЭШ АТЛЕТА ───────────────────────────────────────────────

async def refresh_athlete_cache(db_user_id: int, access_token: str, notify_msg=None,
                                fill_window: bool = False) -> dict | None:
    """
    Обновляет кэш данных атлета (CTL/ATL/TSB, прогнозы, соревнования).
    Считается из окна strava_activities (90 дней), запросов к API нет.
    fill_window=True — сначала заполнить окно из API (список + деталь по каждой
    тренировке, 30-60 сек): только при подключении или по ручной команде.
    """
    try:
        if notify_msg:
            await notify_msg.edit_text(
                "⏳ Загружаю данные из Strava...\n"
                "Это займёт около минуты (только первый раз)"
            )

        # Страховка: окно пустое (первый запуск после деплоя, чистка БД) —
        # заполняем один раз, иначе CTL/ATL посчитаются по одной тренировке.
        if not fill_window and not get_strava_activities(db_user_id):
            logger.info(f"Окно strava_activities пусто для user_id={db_user_id} — заполняю")
            fill_window = True

        athlete_data = await get_full_athlete_data(access_token, db_user_id=db_user_id,
                                                   fill_window=fill_window)

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
    - Быстрые: пробежки за 14 дней + острая нагрузка за 48 ч — из окна
      strava_activities (обновляется вебхуком), без запросов к API
    - Медленные (из кэша): CTL/ATL/TSB, прогнозы Риегеля, последнее соревнование
    Нет кэша или окно пустое → разовое заполнение окна (fill_window) + пересчёт кэша.
    """
    from strava import get_recent_runs, analyze_fitness, get_recent_48h_load

    # ── Медленные данные (из кэша); при пустом окне — заполнить ──
    cache = get_athlete_cache(db_user_id)
    athlete_data = None
    if not cache or not get_strava_activities(db_user_id):
        logger.info(f"Кэш или окно Strava отсутствует для user_id={db_user_id}, заполняю...")
        athlete_data = await refresh_athlete_cache(db_user_id, access_token, fill_window=True)

    # ── Быстрые данные (из окна, 0 запросов к Strava API) ─────
    try:
        runs, load_48h = await asyncio.gather(
            get_recent_runs(access_token, days=14, db_user_id=db_user_id),
            get_recent_48h_load(access_token, db_user_id=db_user_id),
        )
        fitness = analyze_fitness(runs)
        fitness["load_48h"] = load_48h
    except Exception as e:
        logger.error(f"Ошибка получения пробежек: {e}")
        fitness = {"summary": "Нет данных", "total_km": 0, "run_count": 0,
                   "avg_pace": "—", "avg_hr": None, "fatigue_level": "unknown",
                   "load_48h": None}

    slow = athlete_data or cache
    if slow:
        fitness["training_load"] = slow["training_load"]
        fitness["predictions"]   = slow["predictions"]
        fitness["last_race"]     = slow["last_race"]

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
    if get_token(db_user_id, "coros_mcp"):
        try:
            import coros_mcp as _cm
            data = await _cm.get_full_data(db_user_id)
            if data:
                return data
        except Exception as e:
            logger.error(f"COROS (новый) fitness data error for {db_user_id}: {e}")
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

    if get_token(db_user_id, "coros_mcp"):
        try:
            import coros_mcp as _cm
            val = await _cm.get_vo2max(db_user_id)
            if val is not None:
                return float(val), "coros_mcp", "COROS"
        except Exception as e:
            logger.warning(f"VO2max COROS MCP fetch error for uid={db_user_id}: {e}")

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
