import asyncio
from datetime import date, timedelta

_SERVICE = "garmin"


def _load_token(db_user_id: int) -> str | None:
    from database import get_token
    data = get_token(db_user_id, _SERVICE)
    return data["access_token"] if data else None


def _save_token(db_user_id: int, token_json: str) -> None:
    from database import save_token
    save_token(db_user_id, _SERVICE, token_json)


def _build_client(token_json: str):
    from garminconnect import Garmin
    client = Garmin()
    client.login(tokenstore=token_json)
    return client


async def connect(db_user_id: int, email: str, password: str) -> bool:
    """Авторизация в Garmin Connect, сохраняет токен в БД."""
    def _login():
        from garminconnect import Garmin
        client = Garmin(email=email, password=password)
        client.login()
        return client.client.dumps()

    token_json = await asyncio.to_thread(_login)
    _save_token(db_user_id, token_json)
    print(f"Garmin: авторизован user_id={db_user_id}")
    return True


async def _reauth(db_user_id: int):
    """Повторная авторизация Garmin через сохранённые credentials.

    Возвращает свежий клиент или None. По образцу COROS._reauth.
    """
    from database import get_user_profile
    profile = get_user_profile(db_user_id)
    if not profile:
        return None
    email = profile.get("garmin_email")
    password = profile.get("garmin_password")
    if not email or not password:
        print(f"Garmin re-auth: нет сохранённых credentials для user_id={db_user_id}")
        return None
    try:
        def _login():
            from garminconnect import Garmin
            client = Garmin(email=email, password=password)
            client.login()
            return client
        client = await asyncio.to_thread(_login)
        # сохраняем свежий токен
        token_json = client.client.dumps()
        _save_token(db_user_id, token_json)
        print(f"Garmin: re-auth успешен для user_id={db_user_id}")
        return client
    except Exception as e:
        print(f"Garmin re-auth error для user_id={db_user_id}: {e}")
        return None


async def _client(db_user_id: int):
    token_json = _load_token(db_user_id)
    if not token_json:
        return None
    # Пробуем построить клиент из сохранённого токена
    try:
        return await asyncio.to_thread(_build_client, token_json)
    except Exception as e:
        # Токен протух (401 / Failed to retrieve social profile) — релогин по credentials
        print(f"Garmin: токен невалиден для user_id={db_user_id} ({str(e)[:60]}), пробуем re-auth...")
        return await _reauth(db_user_id)


async def get_vo2max(db_user_id: int) -> float | None:
    client = await _client(db_user_id)
    if not client:
        return None

    def _fetch():
        # Garmin обновляет метрику не каждый день — ищем последнее доступное значение
        for days_back in range(7):
            d = (date.today() - timedelta(days=days_back)).isoformat()
            try:
                data = client.get_max_metrics(d)
                if data and isinstance(data, list):
                    generic = data[0].get("generic") or {}
                    val = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
                    if val:
                        return float(val)
            except Exception:
                continue
        return None

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"Garmin get_vo2max error: {e}")
        return None


async def get_training_status(db_user_id: int) -> dict | None:
    client = await _client(db_user_id)
    if not client:
        return None

    def _fetch():
        today = date.today().isoformat()
        data = client.get_training_status(today)
        if not data:
            return None
        # Ответ может быть dict или list
        item = data[0] if isinstance(data, list) else data
        return {
            "status": item.get("trainingStatus") or item.get("status"),
            "load": item.get("trainingLoad"),
            "acute_load": item.get("acuteLoad"),
            "chronic_load": item.get("chronicLoad"),
        }

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"Garmin get_training_status error: {e}")
        return None


async def get_training_readiness(db_user_id: int) -> dict | None:
    client = await _client(db_user_id)
    if not client:
        return None

    def _fetch():
        today = date.today().isoformat()
        data = client.get_training_readiness(today)
        if not data:
            return None
        # Ответ — список
        item = data[0] if isinstance(data, list) else data
        score = item.get("score")
        level_raw = (item.get("level") or "").upper()
        level_map = {
            "POOR": "low", "LOW": "low",
            "FAIR": "low",
            "MODERATE": "moderate",
            "GOOD": "moderate",
            "HIGH": "high",
            "PRIME": "high",
            "OPTIMAL": "high",
        }
        level = level_map.get(level_raw, level_raw.lower() or "unknown")
        # Собираем значимые факторы
        factors = []
        factor_fields = [
            ("sleepScoreFactorFeedback", "Сон"),
            ("recoveryTimeFactorFeedback", "Восстановление"),
            ("hrvFactorFeedback", "ВСР"),
            ("acwrFactorFeedback", "Нагрузка"),
            ("stressHistoryFactorFeedback", "Стресс"),
        ]
        for field, label in factor_fields:
            val = item.get(field, "")
            if val and val not in ("GOOD", "OPTIMAL"):
                factors.append(f"{label}: {val.lower()}")
        return {"score": score, "level": level, "factors": factors}

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"Garmin get_training_readiness error: {e}")
        return None


async def get_body_battery(db_user_id: int) -> int | None:
    client = await _client(db_user_id)
    if not client:
        return None

    def _fetch():
        today = date.today().isoformat()
        data = client.get_user_summary(today)
        if not data:
            return None
        val = data.get("bodyBatteryMostRecentValue")
        return int(val) if val is not None else None

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"Garmin get_body_battery error: {e}")
        return None


async def get_hrv_status(db_user_id: int) -> dict | None:
    client = await _client(db_user_id)
    if not client:
        return None

    def _fetch():
        today = date.today().isoformat()
        data = client.get_hrv_data(today)
        if not data:
            return None
        summary = data.get("hrvSummary", data)
        return {
            "hrv_weekly_avg": summary.get("weeklyAvg"),
            "hrv_last_night": summary.get("lastNightAvg"),
            "status": summary.get("status", "UNBALANCED"),
        }

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"Garmin get_hrv_status error: {e}")
        return None


async def get_lactate_threshold(db_user_id: int) -> dict | None:
    """Возвращает {pace: '4:17', hr: 174} или None."""
    client = await _client(db_user_id)
    if not client:
        return None

    def _fetch():
        data = client.get_lactate_threshold()
        if not data:
            return None
        shr = data.get("speed_and_heart_rate") or {}
        speed_raw = shr.get("speed")
        hr = shr.get("heartRate")
        if not speed_raw or not hr:
            return None
        # Garmin хранит скорость в единицах × 0.1 м/с
        speed_ms = speed_raw * 10
        pace_sec = 1000 / speed_ms
        minutes = int(pace_sec // 60)
        seconds = int(pace_sec % 60)
        return {"pace": f"{minutes}:{seconds:02d}", "hr": int(hr)}

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"Garmin get_lactate_threshold error: {e}")
        return None


async def upload_workout(db_user_id: int, workout_json: dict) -> bool:
    """Uploads a workout plan to Garmin Connect. Returns True on success."""
    client = await _client(db_user_id)
    if not client:
        return False

    def _do():
        return client.upload_workout(workout_json)

    try:
        await asyncio.to_thread(_do)
        return True
    except Exception as e:
        print(f"Garmin upload_workout error: {e}")
        return False


def _format_garmin_load(acute, chronic, tsb) -> str:
    parts = []
    if acute is not None:
        parts.append(f"острая {int(acute)}")
    if chronic is not None:
        parts.append(f"хроническая {int(chronic)}")
    if tsb is not None:
        if tsb > 5:
            form = "хорошая форма"
        elif tsb < -10:
            form = "усталость"
        else:
            form = "норма"
        parts.append(f"баланс {tsb:+.0f} ({form})")
    return ", ".join(parts) if parts else ""


async def get_training_load(db_user_id: int) -> dict | None:
    """Возвращает Training Load из Garmin: острая/хроническая нагрузка, TSB-эквивалент."""
    data = await get_training_status(db_user_id)
    if not data:
        return None
    acute = data.get("acute_load")
    chronic = data.get("chronic_load")
    if acute is None and chronic is None:
        return None
    tsb = round(float(chronic) - float(acute), 1) if (acute and chronic) else None
    return {
        "source": "garmin",
        "acute": acute,
        "chronic": chronic,
        "tsb": tsb,
        "summary": _format_garmin_load(acute, chronic, tsb),
    }


async def get_activities_48h(db_user_id: int) -> dict | None:
    """Возвращает сводку пробежек за последние 48 часов из Garmin."""
    client = await _client(db_user_id)
    if not client:
        return None

    def _fetch():
        from datetime import datetime as _dt, timezone as _tz
        end_dt = _dt.now(_tz.utc)
        start_dt = end_dt - timedelta(hours=48)
        acts = client.get_activities_by_date(
            start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"), "running"
        )
        if not acts:
            return {"sessions_48h": 0, "km_48h": 0.0, "last_activity_hours_ago": None, "suffer_48h": 0}

        sessions = len(acts)
        total_dist_m = sum((a.get("distance") or 0) for a in acts)
        total_km = round(total_dist_m / 1000, 1)
        total_dur_s = sum((a.get("duration") or a.get("movingDuration") or 0) for a in acts)

        # Средний темп (мин:сек на км)
        avg_pace = None
        if total_dist_m > 0 and total_dur_s > 0:
            pace_sec = total_dur_s / (total_dist_m / 1000)
            avg_pace = f"{int(pace_sec) // 60}:{int(pace_sec) % 60:02d}"

        last_hours = None
        for a in acts:
            ts = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
            try:
                act_dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
                if act_dt.tzinfo is None:
                    act_dt = act_dt.replace(tzinfo=_tz.utc)
                h = (end_dt - act_dt).total_seconds() / 3600
                if last_hours is None or h < last_hours:
                    last_hours = h
            except Exception:
                pass

        return {
            "sessions_48h": sessions,
            "km_48h": total_km,
            "avg_pace": avg_pace,
            "last_activity_hours_ago": round(last_hours) if last_hours is not None else None,
            "suffer_48h": 0,
        }

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"Garmin get_activities_48h error: {e}")
        return None


async def get_last_race(db_user_id: int) -> dict | None:
    """Возвращает последнее соревнование за 90 дней.

    Garmin надёжно определяет тип «гонка» через activityType.typeKey.
    Фильтруем только явные типы — без fallback по дистанции.
    """
    client = await _client(db_user_id)
    if not client:
        return None

    def _fetch():
        end = date.today()
        start = end - timedelta(days=90)
        acts = client.get_activities_by_date(start.isoformat(), end.isoformat(), "running")
        if not acts:
            return None

        races = [
            a for a in acts
            if a.get("activityType", {}).get("typeKey", "") in ("running_race", "track_running")
        ]
        if not races:
            return None

        from datetime import datetime as _dt, timezone as _tz

        def _parse_ts(a):
            ts = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
            try:
                dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
                return dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)
            except Exception:
                return _dt.min.replace(tzinfo=_tz.utc)

        races.sort(key=_parse_ts, reverse=True)
        last = races[0]

        dist_km = round((last.get("distance") or 0) / 1000, 1)
        race_date = _parse_ts(last).date()
        days_since = (end - race_date).days
        recovery_days = max(2, round(dist_km / 3))
        recovery_until = (race_date + timedelta(days=recovery_days)).isoformat()

        return {
            "name": last.get("activityName", f"Забег {dist_km} км"),
            "date": race_date.isoformat(),
            "distance_km": dist_km,
            "days_since": days_since,
            "recovery_days": recovery_days,
            "recovery_until": recovery_until,
            "still_recovering": days_since < recovery_days,
        }

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"Garmin get_last_race error: {e}")
        return None


async def get_best_efforts(db_user_id: int) -> dict | None:
    """Оценивает лучшие темпы на 5 км / 10 км по активностям за 180 дней."""
    client = await _client(db_user_id)
    if not client:
        return None

    def _fetch():
        end = date.today()
        start = end - timedelta(days=180)
        acts = client.get_activities_by_date(start.isoformat(), end.isoformat(), "running")
        if not acts:
            return None

        best_5k = None   # лучший темп (сек/км) в активностях диапазона 4.5–6 км
        best_10k = None  # лучший темп в диапазоне 9–11.5 км

        for a in acts:
            dist = a.get("distance") or 0
            dur = a.get("duration") or 0
            if dist <= 0 or dur <= 0:
                continue
            pace = dur / dist * 1000  # сек/км

            if 4500 <= dist <= 6000:
                if best_5k is None or pace < best_5k:
                    best_5k = pace
            elif 9000 <= dist <= 11500:
                if best_10k is None or pace < best_10k:
                    best_10k = pace

        predictions = {}
        if best_5k:
            total_s = int(best_5k * 5)
            m, s = divmod(total_s, 60)
            pm = int(best_5k // 60); ps = int(best_5k % 60)
            predictions["5km"] = {"time": f"0:{m:02d}:{s:02d}", "pace": f"{pm}:{ps:02d}", "source": "garmin"}
        if best_10k:
            total_s = int(best_10k * 10)
            m, s = divmod(total_s, 60)
            pm = int(best_10k // 60); ps = int(best_10k % 60)
            predictions["10km"] = {"time": f"0:{m:02d}:{s:02d}", "pace": f"{pm}:{ps:02d}", "source": "garmin"}

        return predictions if predictions else None

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"Garmin get_best_efforts error: {e}")
        return None


async def get_full_data(db_user_id: int, force_static: bool = False) -> dict | None:
    """Fetches Garmin data with smart caching:
    - Training Readiness, Body Battery, HRV: always fresh (меняются каждый день)
    - VO2max, Training Status: только если кэш >24 ч или force_static=True
    """
    from datetime import datetime as _dt
    from database import get_user_profile

    # Определяем нужен ли свежий запрос статичных данных
    need_static = force_static
    if not need_static:
        try:
            profile = get_user_profile(db_user_id)
            updated = (profile or {}).get("vo2max_updated_at") or ""
            if updated:
                need_static = (_dt.now() - _dt.fromisoformat(updated)).total_seconds() > 86400
            else:
                need_static = True
        except Exception:
            need_static = True

    # Динамичные данные — всегда
    dynamic = await asyncio.gather(
        get_training_readiness(db_user_id),
        get_body_battery(db_user_id),
        get_hrv_status(db_user_id),
        return_exceptions=True,
    )
    readiness, body_battery, hrv = dynamic

    out = {"source": "garmin"}
    if not isinstance(readiness, Exception) and readiness:
        out["training_readiness"] = readiness
    if not isinstance(body_battery, Exception) and body_battery is not None:
        out["body_battery"] = body_battery
    if not isinstance(hrv, Exception) and hrv:
        out["hrv"] = hrv

    # Статичные данные — только если устарели
    if need_static:
        static = await asyncio.gather(
            get_vo2max(db_user_id),
            get_training_status(db_user_id),
            return_exceptions=True,
        )
        vo2max, training_status = static
        if not isinstance(vo2max, Exception) and vo2max is not None:
            out["vo2max"] = vo2max
        if not isinstance(training_status, Exception) and training_status:
            out["training_status"] = training_status

    return out if len(out) > 1 else None

# ── Слой 1.1: загрузка сырых данных ──────────────────────────

async def fetch_raw(db_user_id: int) -> dict | None:
    """Слой 1.1: тянет сырые данные Garmin и сохраняет в raw_service_data as is.

    Собирает все основные показатели в один объект, без обработки.
    Парсинг — слой 2 (data_normalizer).
    """
    import json
    import database as db

    (vo2max, training_status, training_readiness, body_battery,
     hrv_status, lactate_threshold, training_load, activities_48h,
     best_efforts) = await asyncio.gather(
        get_vo2max(db_user_id),
        get_training_status(db_user_id),
        get_training_readiness(db_user_id),
        get_body_battery(db_user_id),
        get_hrv_status(db_user_id),
        get_lactate_threshold(db_user_id),
        get_training_load(db_user_id),
        get_activities_48h(db_user_id),
        get_best_efforts(db_user_id),
        return_exceptions=True,
    )

    def _clean(x):
        return None if isinstance(x, Exception) else x

    raw = {
        "vo2max":             _clean(vo2max),
        "training_status":    _clean(training_status),
        "training_readiness": _clean(training_readiness),
        "body_battery":       _clean(body_battery),
        "hrv_status":         _clean(hrv_status),
        "lactate_threshold":  _clean(lactate_threshold),
        "training_load":      _clean(training_load),
        "activities_48h":     _clean(activities_48h),
        "best_efforts":       _clean(best_efforts),
    }

    if not any(v is not None for v in raw.values()):
        print(f"Garmin fetch_raw: нет данных для user_id={db_user_id}")
        return None

    db.save_raw_service_data(db_user_id, "garmin", json.dumps(raw, ensure_ascii=False, default=str))
    got = [k for k, v in raw.items() if v is not None]
    print(f"Garmin fetch_raw: сохранено для user_id={db_user_id} ({', '.join(got)})")
    return raw

# ── Профиль: пол и дата рождения (статичные данные) ──────────

async def get_profile(db_user_id: int) -> dict | None:
    """Пол и дата рождения из Garmin в едином формате.

    Возвращает {gender: 'male'/'female', birthdate: 'YYYY-MM-DD'} или None.
    Garmin userData.gender = MALE/FEMALE, userData.birthDate = '1981-06-20'.
    """
    client = await _client(db_user_id)
    if not client:
        return None

    def _fetch():
        try:
            prof = client.get_user_profile()
            return (prof or {}).get("userData") or {}
        except Exception as e:
            print(f"Garmin get_profile error: {e}")
            return {}

    ud = await asyncio.to_thread(_fetch)
    if not ud:
        return None

    result: dict = {}
    gender = ud.get("gender")
    if gender:
        result["gender"] = "female" if str(gender).upper() == "FEMALE" else "male"
    bdate = ud.get("birthDate")
    if bdate:
        result["birthdate"] = str(bdate)[:10]  # уже YYYY-MM-DD

    return result or None
