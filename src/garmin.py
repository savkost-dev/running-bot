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


async def _client(db_user_id: int):
    token_json = _load_token(db_user_id)
    if not token_json:
        return None
    return _build_client(token_json)


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


async def get_full_data(db_user_id: int) -> dict | None:
    results = await asyncio.gather(
        get_vo2max(db_user_id),
        get_training_status(db_user_id),
        get_training_readiness(db_user_id),
        get_body_battery(db_user_id),
        get_hrv_status(db_user_id),
        return_exceptions=True,
    )
    vo2max, training_status, readiness, body_battery, hrv = results
    out = {"source": "garmin"}
    if not isinstance(vo2max, Exception) and vo2max is not None:
        out["vo2max"] = vo2max
    if not isinstance(training_status, Exception) and training_status:
        out["training_status"] = training_status
    if not isinstance(readiness, Exception) and readiness:
        out["training_readiness"] = readiness
    if not isinstance(body_battery, Exception) and body_battery is not None:
        out["body_battery"] = body_battery
    if not isinstance(hrv, Exception) and hrv:
        out["hrv"] = hrv
    return out if len(out) > 1 else None
