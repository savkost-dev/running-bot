"""Данные восстановления (recovery).

Вынесено из bot.py без изменения логики. Чистый лист: тянет только из
database/сервисов/data_normalizer, обратных импортов в bot.py не имеет.

Функции:
- _update_garmin_recovery_from_raw — обновляет garmin_recovery_cache из raw
- _fetch_garmin_recovery          — живой запрос Garmin + кэш
- _get_recovery_data              — выбор источника (Whoop → Garmin → COROS → Polar)
- _garmin_observation_end         — wellnessEndTimeLocal из сырья Garmin
- _get_unified_recovery           — live (force_fresh) или утренний снимок
- _recovery_scenario              — тексты сценария восстановления для промпта/сообщения
"""
import asyncio
import logging

from database import (
    get_preferences, get_token,
    get_garmin_recovery_cache, save_garmin_recovery_cache,
)

logger = logging.getLogger(__name__)


def _update_garmin_recovery_from_raw(db_user_id: int, raw: dict) -> None:
    """Обновляет garmin_recovery_cache из сырого ответа garmin.fetch_raw.

    Обеспечивает совместимость с _get_recovery_data, который читает из garmin_recovery_cache.
    """
    result: dict = {"source": "garmin"}

    # Body Battery и ЧСС покоя из user_summary
    us = raw.get("user_summary") or {}
    bb = us.get("bodyBatteryMostRecentValue")
    if bb is not None:
        result["body_battery"] = int(bb)
        result["recovery_score"] = int(bb)

    # HRV из hrv_data
    hrv_raw = raw.get("hrv_data") or {}
    summary = hrv_raw.get("hrvSummary") or {}
    if summary:
        last_night = summary.get("lastNightAvg")
        weekly = summary.get("weeklyAvg")
        if last_night:
            result["hrv"] = float(last_night)
        if weekly:
            result["hrv_weekly_avg"] = float(weekly)
        result["hrv_status"] = summary.get("status")

    # Training Readiness
    tr_raw = raw.get("training_readiness")
    if tr_raw:
        item = tr_raw[0] if isinstance(tr_raw, list) else tr_raw
        if isinstance(item, dict) and item.get("score") is not None:
            level_raw = (item.get("level") or "").upper()
            level_map = {"POOR": "low", "LOW": "low", "FAIR": "low",
                         "MODERATE": "moderate", "GOOD": "moderate",
                         "HIGH": "high", "PRIME": "high", "OPTIMAL": "high"}
            result["training_readiness"] = {
                "score": item["score"],
                "level": level_map.get(level_raw, level_raw.lower() or "unknown"),
                "factors": [],
            }

    if len(result) > 1:
        save_garmin_recovery_cache(db_user_id, result)


async def _fetch_garmin_recovery(db_user_id: int) -> dict | None:
    """Запрашивает данные восстановления из Garmin API и сохраняет в кэш."""
    from garmin import get_body_battery_with_sync, get_hrv_status, get_training_readiness
    try:
        bb_result, hrv_data, readiness = await asyncio.gather(
            get_body_battery_with_sync(db_user_id),
            get_hrv_status(db_user_id),
            get_training_readiness(db_user_id),
            return_exceptions=True,
        )
        result = {"source": "garmin"}
        if not isinstance(bb_result, Exception) and bb_result is not None:
            body_battery, synced_at = bb_result
            if body_battery is not None:
                result["body_battery"] = body_battery
                result["recovery_score"] = body_battery
            if synced_at:
                result["synced_at"] = synced_at
        if not isinstance(hrv_data, Exception) and hrv_data:
            result["hrv"] = hrv_data.get("hrv_last_night")
            result["hrv_weekly_avg"] = hrv_data.get("hrv_weekly_avg")
            result["hrv_status"] = hrv_data.get("status")
        if not isinstance(readiness, Exception) and readiness:
            result["training_readiness"] = readiness
        if len(result) > 1:
            save_garmin_recovery_cache(db_user_id, result)
            return result
    except Exception as e:
        logger.error(f"Garmin recovery fetch error for user {db_user_id}: {e}")
    return None


async def _get_recovery_data(db_user_id: int, force_fresh: bool = False) -> dict | None:
    """Возвращает данные восстановления (Whoop → Garmin).
    force_fresh=True — пропускает кэш Garmin, всегда запрашивает API.
    Используй force_fresh=True для /workout и /long, чтобы TR и Body Battery были актуальными.
    """
    prefs = get_preferences(db_user_id)
    use_garmin = prefs.get("use_garmin_recovery", True) if prefs else True
    has_garmin = bool(get_token(db_user_id, "garmin"))

    # Whoop — приоритет
    whoop_data = None
    from whoop import get_full_recovery_data, ensure_valid_token as whoop_valid_token
    try:
        access_token = await whoop_valid_token(db_user_id)
        if access_token:
            whoop_data = await get_full_recovery_data(access_token)
    except Exception as e:
        logger.error(f"Whoop error for user {db_user_id}: {e}")

    if whoop_data:
        # Если Garmin носят постоянно — Training Readiness поверх Whoop
        if use_garmin and has_garmin:
            try:
                tr = None
                synced_at = None
                if not force_fresh:
                    cached = get_garmin_recovery_cache(db_user_id)
                    tr = cached.get("training_readiness") if cached else None
                if not tr:
                    from garmin import get_training_readiness, get_body_battery_with_sync
                    if force_fresh:
                        tr, bb_result = await asyncio.gather(
                            get_training_readiness(db_user_id),
                            get_body_battery_with_sync(db_user_id),
                        )
                        if bb_result and not isinstance(bb_result, Exception):
                            _, synced_at = bb_result
                    else:
                        tr = await get_training_readiness(db_user_id)
                if tr:
                    whoop_data["training_readiness"] = tr
                if synced_at:
                    whoop_data["synced_at"] = synced_at
            except Exception as e:
                logger.error(f"Garmin TR error for user {db_user_id}: {e}")
        return whoop_data

    # Garmin — кэш (8 ч) или живой запрос
    if use_garmin and has_garmin:
        if not force_fresh:
            cached = get_garmin_recovery_cache(db_user_id)
            if cached:
                return cached
        garmin_result = await _fetch_garmin_recovery(db_user_id)
        if garmin_result:
            return garmin_result

    # COROS — третий приоритет
    if get_token(db_user_id, "coros"):
        try:
            import coros as _coros
            result = await _coros.get_recovery_for_prompt(db_user_id)
            if result:
                return result
        except Exception as e:
            logger.error(f"COROS recovery error for user {db_user_id}: {e}")

    # Polar — четвёртый приоритет
    if get_token(db_user_id, "polar"):
        try:
            import polar as _polar
            result = await _polar.get_recovery_for_prompt(db_user_id)
            if result:
                return result
        except Exception as e:
            logger.error(f"Polar recovery error for user {db_user_id}: {e}")

    return None


def _garmin_observation_end(db_user_id: int) -> str | None:
    """wellnessEndTimeLocal из сырья Garmin — до какого момента есть данные (локальное время).
    Для остальных сервисов поля нет — вызывающий оставляет синк как конец наблюдения."""
    import json as _json
    from database import get_raw_service_data, get_token
    if not get_token(db_user_id, "garmin"):
        return None
    row = get_raw_service_data(db_user_id, "garmin")
    if not row:
        return None
    try:
        us = (_json.loads(row["raw_json"]) or {}).get("user_summary") or {}
        return us.get("wellnessEndTimeLocal") or None
    except Exception:
        return None


async def _get_unified_recovery(db_user_id: int, force_fresh: bool = True) -> dict | None:
    from database import get_unified_data
    from data_normalizer import UnifiedUserData

    if force_fresh:
        # Будущая тренировка — свежие данные, метка из живого запроса
        recovery = await _get_recovery_data(db_user_id, force_fresh=True)
        if not recovery:
            return None
        # data_fetched_at: берём lastSyncTimestampGMT из свежего запроса Garmin
        # (сохранён в synced_at в _fetch_garmin_recovery).
        # Если нет (Whoop, COROS, Polar) — фолбэк на unified_cache как раньше.
        synced_at = recovery.pop("synced_at", None)
        if synced_at:
            recovery["data_fetched_at"] = synced_at
        else:
            try:
                row = get_unified_data(db_user_id, max_age_hours=20)
                if row:
                    u = UnifiedUserData.from_json(row["unified_json"])
                    dd = u.data_dates or {}
                    recovery["data_fetched_at"] = (dd.get("garmin_synced_at")
                        or dd.get("garmin_fetched") or dd.get("coros_fetched")
                        or dd.get("polar_fetched") or dd.get("strava_fetched")
                        or row.get("updated_at"))
            except Exception:
                pass
        # Для Garmin конец наблюдения = wellnessEndTimeLocal (локальное); иначе синк выше
        _obs_end = _garmin_observation_end(db_user_id)
        if _obs_end:
            recovery["data_fetched_at"] = _obs_end
        return recovery

    # Прошедшая тренировка — данные ИЗ unified_cache (утренний снимок)
    try:
        row = get_unified_data(db_user_id, max_age_hours=20)
        if not row:
            return await _get_recovery_data(db_user_id, force_fresh=False)
        u = UnifiedUserData.from_json(row["unified_json"])
        dd = u.data_dates or {}
        _data_fetched = (dd.get("garmin_synced_at")
            or dd.get("garmin_fetched") or dd.get("coros_fetched")
            or dd.get("polar_fetched") or dd.get("strava_fetched")
            or row.get("updated_at"))
        # Для Garmin конец наблюдения = wellnessEndTimeLocal; иначе синк
        _obs_end = _garmin_observation_end(db_user_id)
        return {
            "source":                  "unified_cache",
            "recovery_score":          u.s3_recovery_daily,
            "training_readiness":      u.s3_training_readiness,
            "training_readiness_at":   u.s3_training_readiness_at,
            "recovery_total":          u.s3_recovery_total,
            "hrv":                     u.s3_hrv,
            "rhr":                     u.s3_rhr,
            "body_battery":            u.s3_body_battery,
            "sleep_hours":             u.s3_sleep_hours,
            "data_fetched_at":         _obs_end or _data_fetched,
        }
    except Exception:
        return await _get_recovery_data(db_user_id, force_fresh=False)


def _recovery_scenario(workout_dict: dict, data_fetched_at: str | None) -> dict:
    """Определяет сценарий восстановления и возвращает тексты для промпта и сообщения."""
    from datetime import datetime, timezone, timedelta
    import re
    MSK = timezone(timedelta(hours=3))
    is_past = workout_dict.get("is_past", False)

    if is_past:
        time_str = ""
        if data_fetched_at:
            try:
                from datetime import datetime, timezone, timedelta
                MSK = timezone(timedelta(hours=3))
                dt = datetime.fromisoformat(data_fetched_at.replace("Z", "+00:00"))
                # naive (без tz) = wellnessEndTimeLocal, УЖЕ МСК — не конвертируем; aware (Z) — в МСК
                dt_msk = dt.astimezone(MSK) if dt.tzinfo else dt.replace(tzinfo=MSK)
                time_str = dt_msk.strftime("%H:%M МСК %d.%m")
            except Exception:
                pass
        cache_note = f" Данные восстановления из кэша на {time_str}." if time_str else ""
        return {
            "scenario": 3,
            "hours_until": None,
            "workout_time_str": None,
            "user_text": f"📅 Тренировка уже состоялась — рекомендация ознакомительная.{cache_note}",
            "prompt_text": f"Тренировка уже состоялась — расчёт ретроспективный.{cache_note}",
            "needs_forecast": False,
        }

    # Парсим время тренировки (МСК)
    workout_date = workout_dict.get("workout_date", "")
    schedule = workout_dict.get("schedule", "") or ""
    m = re.search(r'(\d{1,2}:\d{2})', schedule)
    start_time = m.group(1) if m else "07:00"
    try:
        workout_dt = datetime.strptime(
            f"{workout_date} {start_time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=MSK)
    except Exception:
        workout_dt = None

    workout_time_str = workout_dt.strftime("%d.%m в %H:%M") if workout_dt else workout_date

    # Метка времени данных
    sync_str = ""
    data_dt = datetime.now(MSK)  # фолбэк если нет data_fetched_at
    if data_fetched_at:
        try:
            dt = datetime.fromisoformat(data_fetched_at.replace("Z", "+00:00"))
            # naive (без tz) = wellnessEndTimeLocal, УЖЕ МСК — не конвертируем; aware (Z) — в МСК
            data_dt = dt.astimezone(MSK) if dt.tzinfo else dt.replace(tzinfo=MSK)
            sync_str = f"Данные синхронизированы: {data_dt.strftime('%H:%M МСК %d.%m')}. "
        except Exception:
            pass

    # Разница от времени данных до старта тренировки
    hours_until = (
        round((workout_dt - data_dt).total_seconds() / 3600, 1)
        if workout_dt else None
    )

    needs_forecast = True
    if hours_until is not None and hours_until > 6:
        user_text = (
            f"⏱ {sync_str}"
            f"От данных до старта ~{hours_until:.0f} ч — "
            f"{'за ночь восстановишься, ' if hours_until > 8 else ''}"
            f"рекомендация с учётом прогноза к {start_time} МСК."
        )
        prompt_text = (
            f"{sync_str}"
            f"Тренировка: {workout_time_str} МСК. "
            f"От времени данных до старта ~{hours_until:.0f} ч — "
            f"за это время восстановление продолжится. "
            f"Давай рекомендацию из расчёта восстановления к старту, "
            f"не из текущего момента. "
            f"Верни в JSON поле recovery_forecast: прогноз восстановления "
            f"к моменту тренировки (1-2 предложения)."
        )
    else:
        user_text = (
            f"⏱ {sync_str}"
            + (f"До старта ~{hours_until:.0f} ч — данные актуальны."
               if hours_until is not None else "Данные актуальны.")
        )
        prompt_text = (
            f"{sync_str}"
            f"Тренировка: {workout_time_str} МСК. "
            f"Данные восстановления актуальны на момент старта. "
            f"Верни в JSON поле recovery_forecast: краткая оценка "
            f"восстановления к старту (1-2 предложения)."
        )

    return {
        "scenario": 2 if (hours_until and hours_until > 9) else 1,
        "hours_until": hours_until,
        "workout_time_str": workout_time_str,
        "user_text": user_text,
        "prompt_text": prompt_text,
        "needs_forecast": needs_forecast,
    }
