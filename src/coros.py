"""
COROS Connect integration — прямые HTTP запросы к неофициальному API.
Reverse-engineered: https://teamapi.coros.com

Авторизация: POST /account/login с MD5-хэшем пароля → accessToken
Данные:      заголовок  accesstoken: TOKEN
"""
import hashlib
import asyncio
import json as _json
from datetime import date, timedelta

import aiohttp

_BASE_URL = "https://teamapi.coros.com"
_SERVICE  = "coros"
_TIMEOUT  = aiohttp.ClientTimeout(total=30)


# ── Утилиты ──────────────────────────────────────────────────

def _md5(text: str) -> str:
    """COROS требует MD5-хэш пароля перед отправкой."""
    return hashlib.md5(text.encode()).hexdigest()


def _load_token(db_user_id: int) -> str | None:
    from database import get_token
    data = get_token(db_user_id, _SERVICE)
    return data["access_token"] if data else None


def _save_token(db_user_id: int, access_token: str) -> None:
    from database import save_token
    save_token(db_user_id, _SERVICE, access_token)


def _headers(access_token: str) -> dict:
    return {
        "accesstoken": access_token,
        "content-type": "application/json",
    }


def _ok(data: dict | None) -> bool:
    """Проверяем успешный ответ COROS API."""
    if not data:
        return False
    return str(data.get("result", "")) == "0000"


# ── Авторизация ───────────────────────────────────────────────

async def connect(db_user_id: int, email: str, password: str) -> str:
    """Авторизация в COROS, сохраняет accessToken в БД.

    Возвращает токен или бросает исключение при ошибке.
    """
    payload = {
        "account": email,
        "accountType": 2,
        "pwd": _md5(password),
    }
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(
            f"{_BASE_URL}/account/login",
            json=payload,
            headers={"content-type": "application/json"},
        ) as resp:
            raw = await resp.text()
            try:
                data = _json.loads(raw)
            except Exception:
                raise ValueError(f"COROS вернул не-JSON: {raw[:200]}")

    if not _ok(data):
        msg = data.get("message") or data.get("msg") or _json.dumps(data)[:200]
        raise ValueError(f"COROS авторизация не удалась: {msg}")

    token = (data.get("data") or {}).get("accessToken")
    if not token:
        raise ValueError(f"COROS: accessToken не найден в ответе: {_json.dumps(data)[:200]}")

    _save_token(db_user_id, token)
    print(f"COROS: авторизован user_id={db_user_id}")
    return token


async def _reauth(db_user_id: int) -> str | None:
    """Повторная авторизация через сохранённые credentials."""
    from database import get_user_profile
    profile = get_user_profile(db_user_id)
    if not profile:
        return None
    email = profile.get("coros_email")
    password = profile.get("coros_password")
    if not email or not password:
        return None
    try:
        return await connect(db_user_id, email, password)
    except Exception as e:
        print(f"COROS re-auth error: {e}")
        return None


# ── HTTP хелпер ───────────────────────────────────────────────

async def _get(db_user_id: int, path: str, params: dict | None = None,
               _retry: bool = True) -> dict | None:
    """GET запрос к COROS API с автоматической re-авторизацией."""
    token = _load_token(db_user_id)
    if not token:
        return None
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(
                f"{_BASE_URL}{path}",
                params=params or {},
                headers=_headers(token),
            ) as resp:
                if resp.status == 401 and _retry:
                    # Токен истёк — пробуем переавторизоваться
                    new_token = await _reauth(db_user_id)
                    if new_token:
                        return await _get(db_user_id, path, params, _retry=False)
                    return None
                if resp.status != 200:
                    print(f"COROS GET {path} → HTTP {resp.status}")
                    return None
                raw = await resp.text()
                return _json.loads(raw)
    except Exception as e:
        print(f"COROS GET {path} error: {e}")
        return None


# ── Данные ───────────────────────────────────────────────────

async def get_vo2max(db_user_id: int) -> float | None:
    """VO2max из EvoLab данных за последние 30 дней."""
    today = date.today()
    params = {
        "startDay": (today - timedelta(days=30)).strftime("%Y%m%d"),
        "endDay": today.strftime("%Y%m%d"),
    }

    # Endpoint 1: EvoLab training metrics
    data = await _get(db_user_id, "/analyse/training/load/query", params)
    if _ok(data):
        d = data.get("data") or {}
        # Прямо в data
        for key in ("vo2Max", "vo2max", "maxOxygenUptake", "vo2MaxValue"):
            val = d.get(key)
            if val and float(val) > 0:
                print(f"COROS VO2max={val} (key={key})")
                return round(float(val), 1)
        # В списке дней — берём последнее ненулевое
        for item in reversed(d.get("dataList") or []):
            if not isinstance(item, dict):
                continue
            for key in ("vo2Max", "vo2max", "maxOxygenUptake"):
                val = item.get(key)
                if val and float(val) > 0:
                    return round(float(val), 1)

    # Endpoint 2: daily summary
    data2 = await _get(db_user_id, "/sport/dailySummary/query", params)
    if _ok(data2):
        for item in reversed((data2.get("data") or {}).get("dataList") or []):
            if not isinstance(item, dict):
                continue
            for key in ("vo2Max", "vo2max"):
                val = item.get(key)
                if val and float(val) > 0:
                    return round(float(val), 1)

    return None


async def get_training_load(db_user_id: int) -> dict | None:
    """Острая (7d) и хроническая (42d) нагрузка, TSB."""
    today = date.today()
    params = {
        "startDay": (today - timedelta(days=7)).strftime("%Y%m%d"),
        "endDay": today.strftime("%Y%m%d"),
    }

    data = await _get(db_user_id, "/analyse/training/load/query", params)
    if not _ok(data):
        return None

    d = data.get("data") or {}

    def _f(keys):
        for k in keys:
            v = d.get(k)
            if v is not None:
                return float(v)
        return None

    atl = _f(["acuteTrainingLoad", "atl", "acuteLoad"])
    ctl = _f(["chronicTrainingLoad", "ctl", "chronicLoad"])
    tsb = _f(["trainingStressBalance", "tsb"])
    if tsb is None and ctl is not None and atl is not None:
        tsb = round(ctl - atl, 1)

    print(f"COROS training load: CTL={ctl} ATL={atl} TSB={tsb} | raw_keys={list(d.keys())[:10]}")

    if atl is None and ctl is None:
        return None

    form_text = (
        "свежий" if (tsb or 0) > 5 else
        "перегрузка" if (tsb or 0) < -20 else
        "небольшая усталость"
    )

    return {
        "source": "coros",
        "ctl": ctl,
        "atl": atl,
        "tsb": tsb,
        "form_text": form_text,
        "trend_text": "",
        "summary": (
            f"CTL={ctl}, ATL={atl}, TSB={tsb} ({form_text}) [COROS]"
            if ctl is not None else "Нагрузка COROS: нет данных"
        ),
    }


async def get_training_status(db_user_id: int) -> dict | None:
    """Статус формы из EvoLab."""
    today = date.today()
    params = {
        "startDay": (today - timedelta(days=7)).strftime("%Y%m%d"),
        "endDay": today.strftime("%Y%m%d"),
    }
    data = await _get(db_user_id, "/analyse/training/load/query", params)
    if not _ok(data):
        return None
    d = data.get("data") or {}
    status = d.get("trainingStatus") or d.get("status")
    return {"status": str(status)} if status else None


async def get_hrv_status(db_user_id: int) -> dict | None:
    """HRV за последнюю ночь и среднее за неделю."""
    today = date.today()
    params = {
        "startDay": (today - timedelta(days=7)).strftime("%Y%m%d"),
        "endDay": today.strftime("%Y%m%d"),
    }

    # Endpoint 1: health/hrv
    data = await _get(db_user_id, "/health/hrv/query", params)
    items = None
    if _ok(data):
        d = data.get("data") or {}
        items = d.get("dataList") or (d if isinstance(d, list) else None)

    # Endpoint 2: daily summary (fallback)
    if not items:
        data2 = await _get(db_user_id, "/sport/dailySummary/query", params)
        if _ok(data2):
            d2 = data2.get("data") or {}
            items = d2.get("dataList") or []

    if not items:
        return None

    # Собираем HRV значения
    hrv_values = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("hrv", "hrvValue", "avgHrv", "nightHrv", "rmssd"):
            v = item.get(key)
            if v and float(v) > 0:
                hrv_values.append(float(v))
                break

    if not hrv_values:
        return None

    hrv_last = hrv_values[-1]
    hrv_avg = round(sum(hrv_values) / len(hrv_values), 1)

    print(f"COROS HRV: last={hrv_last} avg={hrv_avg} ({len(hrv_values)} days)")
    return {
        "hrv_last_night": hrv_last,
        "hrv_weekly_avg": hrv_avg,
        "status": "normal",
    }


async def get_recovery(db_user_id: int) -> dict | None:
    """Готовность к тренировке из дневных данных COROS."""
    today = date.today()
    params = {
        "startDay": (today - timedelta(days=3)).strftime("%Y%m%d"),
        "endDay": today.strftime("%Y%m%d"),
    }

    data = await _get(db_user_id, "/sport/dailySummary/query", params)
    if not _ok(data):
        return None

    items = (data.get("data") or {}).get("dataList") or []
    if not items:
        return None

    latest = items[-1] if isinstance(items[-1], dict) else {}

    # COROS выражает восстановление через recoveryTime (часов) или fatigue (%)
    recovery_time_h = latest.get("recoveryTime")
    fatigue_pct = latest.get("fatigue") or latest.get("fatigueRate")

    if fatigue_pct is not None:
        score = max(0, min(100, round(100 - float(fatigue_pct))))
    elif recovery_time_h is not None:
        rt = float(recovery_time_h)
        # 0 ч → 100%, 24+ ч → < 4%
        score = max(0, min(100, round(100 - rt * 4)))
    else:
        score = None

    print(f"COROS recovery: score={score} fatigue={fatigue_pct} recoveryTime={recovery_time_h}")
    return {
        "source": "coros",
        "recovery_score": score,
        "recovery_time_h": float(recovery_time_h) if recovery_time_h else None,
    }


async def get_full_data(db_user_id: int) -> dict | None:
    """Все данные для промта: VO2max, нагрузка, HRV, готовность.

    Возвращает dict совместимый со структурой fitness/recovery для промта,
    или None если COROS не подключён или не отвечает.
    """
    if not _load_token(db_user_id):
        return None

    vo2max, training_load, hrv, recovery = await asyncio.gather(
        get_vo2max(db_user_id),
        get_training_load(db_user_id),
        get_hrv_status(db_user_id),
        get_recovery(db_user_id),
        return_exceptions=True,
    )

    if isinstance(vo2max, Exception):
        vo2max = None
    if isinstance(training_load, Exception):
        training_load = None
    if isinstance(hrv, Exception):
        hrv = None
    if isinstance(recovery, Exception):
        recovery = None

    # Нет ни VO2max ни нагрузки — не возвращаем ничего
    if not vo2max and not training_load:
        print(f"COROS get_full_data: нет данных для user_id={db_user_id}")
        return None

    fitness: dict = {
        "source": "coros",
        "summary": "",
        "total_km": 0,
        "run_count": 0,
        "avg_pace": "—",
        "avg_hr": None,
        "fatigue_level": "unknown",
    }

    if vo2max:
        fitness["vo2max"] = vo2max
        fitness["vo2max_source"] = "COROS"

    if training_load:
        fitness["training_load"] = training_load
        tsb = training_load.get("tsb")
        if tsb is not None:
            fitness["fatigue_level"] = (
                "fresh" if tsb > 5 else "tired" if tsb < -15 else "normal"
            )

    if hrv:
        fitness["hrv_data"] = hrv

    if recovery:
        fitness["recovery"] = recovery

    # Собираем summary
    parts = []
    if vo2max:
        parts.append(f"VO2max {vo2max}")
    if training_load and training_load.get("summary"):
        parts.append(training_load["summary"])
    fitness["summary"] = " | ".join(parts) if parts else "COROS данные получены"

    return fitness


# ── Для recovery блока (совместимость с _get_recovery_data) ───

async def get_recovery_for_prompt(db_user_id: int) -> dict | None:
    """Возвращает recovery dict в формате, совместимом с Whoop/Garmin.
    Используется в _get_recovery_data() как fallback после Garmin.
    """
    if not _load_token(db_user_id):
        return None

    hrv, recovery = await asyncio.gather(
        get_hrv_status(db_user_id),
        get_recovery(db_user_id),
        return_exceptions=True,
    )

    result: dict = {"source": "coros"}

    if not isinstance(hrv, Exception) and hrv:
        result["hrv"] = hrv.get("hrv_last_night")
        result["hrv_weekly_avg"] = hrv.get("hrv_weekly_avg")

    if not isinstance(recovery, Exception) and recovery and recovery.get("recovery_score") is not None:
        result["recovery_score"] = recovery["recovery_score"]

    if len(result) <= 1:  # только "source"
        return None

    return result
