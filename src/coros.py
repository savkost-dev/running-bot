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
                if resp.status in (401, 500) and _retry:
                    # COROS возвращает 500 (а не 401) на протухший токен — пробуем переавторизоваться
                    print(f"COROS GET {path} → HTTP {resp.status}, пробуем re-auth...")
                    new_token = await _reauth(db_user_id)
                    if new_token:
                        return await _get(db_user_id, path, params, _retry=False)
                    print(f"COROS re-auth не удался для user_id={db_user_id}")
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

def _ltsp_to_pace(ltsp_sec: int) -> str:
    """Лактатный порог в сек/км (напр. 244) → '4:04'."""
    return f"{ltsp_sec // 60}:{ltsp_sec % 60:02d}"


async def get_dashboard_data(db_user_id: int) -> dict | None:
    """Данные EvoLab из /dashboard/query.

    Рабочий endpoint (проверено 02.06.2026):
    - ltsp: лактатный порог в сек/км
    - lthr: лактатный порог ЧСС
    - fullRecoveryHours: часов до полного восстановления
    - aerobicEnduranceScore и другие EvoLab-скоры
    """
    data = await _get(db_user_id, "/dashboard/query")
    if not _ok(data):
        return None
    d = data.get("data") or {}
    info = d.get("summaryInfo") or {}
    if not info:
        return None

    result: dict = {"source": "coros_dashboard"}

    # Лактатный порог
    ltsp = info.get("ltsp")
    lthr = info.get("lthr")
    if ltsp and int(ltsp) > 0:
        result["lactate_threshold_pace"] = _ltsp_to_pace(int(ltsp))
    if lthr and int(lthr) > 0:
        result["lactate_threshold_hr"] = int(lthr)

    # Восстановление — нативный recoveryPct (приоритет) или из fullRecoveryHours
    rpc = info.get("recoveryPct")
    if rpc is not None and int(rpc) >= 0:
        result["recovery_score"] = max(0, min(100, int(rpc)))
    frh = info.get("fullRecoveryHours")
    if frh is not None:
        result["full_recovery_hours"] = float(frh)
        if result.get("recovery_score") is None:
            result["recovery_score"] = max(0, min(100, round(100 - float(frh) * 4)))
    rstate = info.get("recoveryState")
    if rstate is not None:
        result["recovery_state"] = int(rstate)  # 1=истощён 2=уставший 3=хороший 4=отличный

    # HRV из sleepHrvData
    hrv_data = info.get("sleepHrvData") or {}
    if isinstance(hrv_data, dict):
        hrv_last = hrv_data.get("avgSleepHrv")
        hrv_base = hrv_data.get("sleepHrvBase")
        if hrv_last and float(hrv_last) > 0:
            result["hrv"] = float(hrv_last)
        if hrv_base and float(hrv_base) > 0:
            result["hrv_baseline"] = float(hrv_base)
        # История HRV за 7 дней
        hrv_list = hrv_data.get("sleepHrvList") or []
        if hrv_list:
            vals = [x.get("avgSleepHrv") for x in hrv_list if x.get("avgSleepHrv")]
            if vals:
                result["hrv_weekly_avg"] = round(sum(vals) / len(vals), 1)

    # ЧСС покоя
    rhr = info.get("rhr")
    if rhr and int(rhr) > 0:
        result["rhr"] = int(rhr)

    # Пульс цикла и макс
    for key, rkey in (("cycleLevelHr", "cycle_level_hr"), ("fitnessMaxHr", "fitness_max_hr"),
                      ("runningLevelHr", "running_level_hr")):
        val = info.get(key)
        if val and int(val) > 0:
            result[rkey] = int(val)

    # EvoLab скоры формы
    for key in ("aerobicEnduranceScore", "anaerobicCapacityScore",
                "lactateThresholdCapacityScore", "anaerobicEnduranceScore",
                "staminaLevel", "staminaLevelChange"):
        val = info.get(key)
        if val is not None:
            result[key] = float(val)

    # VO2max если есть
    for key in ("vo2Max", "vo2max", "maxOxygenUptake"):
        val = info.get(key)
        if val and float(val) > 0:
            result["vo2max"] = round(float(val), 1)
            break

    print(f"COROS dashboard: ltsp={result.get('lactate_threshold_pace')} "
          f"lthr={result.get('lactate_threshold_hr')} "
          f"recovery={result.get('recovery_score')} "
          f"hrv={result.get('hrv')} rhr={result.get('rhr')} "
          f"vo2max={result.get('vo2max')}")
    return result


async def _get_day_detail(db_user_id: int, days: int = 7) -> list[dict]:
    """Дневные метрики из dayDetail — основной рабочий endpoint COROS.

    Возвращает список записей за последние N дней (свежие последними).
    Поля: avg_sleep_hrv, rhr, tired_rate, training_load, ati, cti, vo2max, lthr, ltsp, distance, duration.
    """
    today = date.today()
    params = {
        "startDay": (today - timedelta(days=days)).strftime("%Y%m%d"),
        "endDay": today.strftime("%Y%m%d"),
    }
    data = await _get(db_user_id, "/v2/coros/sport/detail/dayDetail", params)
    if not _ok(data):
        return []
    raw = data.get("data") or {}
    items = raw.get("dataList") or (raw if isinstance(raw, list) else [])
    return [i for i in items if isinstance(i, dict)]


async def get_vo2max(db_user_id: int) -> float | None:
    """VO2max из dayDetail (последние 28 дней) или analyse как fallback."""
    # Основной путь: dayDetail
    items = await _get_day_detail(db_user_id, days=28)
    for item in reversed(items):
        for key in ("vo2max", "vo2Max", "maxOxygenUptake"):
            val = item.get(key)
            if val and float(val) > 0:
                print(f"COROS VO2max={val} (dayDetail, key={key})")
                return round(float(val), 1)

    # Fallback: analyse endpoint
    today = date.today()
    params = {
        "startDay": (today - timedelta(days=28)).strftime("%Y%m%d"),
        "endDay": today.strftime("%Y%m%d"),
    }
    data = await _get(db_user_id, "/analyse/training/load/query", params)
    if _ok(data):
        d = data.get("data") or {}
        for key in ("vo2Max", "vo2max", "maxOxygenUptake", "vo2MaxValue"):
            val = d.get(key)
            if val and float(val) > 0:
                print(f"COROS VO2max={val} (analyse, key={key})")
                return round(float(val), 1)
        for item in reversed(d.get("dataList") or []):
            if not isinstance(item, dict):
                continue
            for key in ("vo2Max", "vo2max", "maxOxygenUptake"):
                val = item.get(key)
                if val and float(val) > 0:
                    return round(float(val), 1)

    return None


async def get_training_load(db_user_id: int) -> dict | None:
    """Острая/хроническая нагрузка из dayDetail (ati/cti) или analyse как fallback."""
    # Основной путь: dayDetail — берём последнюю запись с ati/cti
    items = await _get_day_detail(db_user_id, days=7)
    atl = ctl = tsb = None
    for item in reversed(items):
        ati = item.get("ati")
        cti = item.get("cti")
        tl_ratio = item.get("training_load_ratio") or item.get("trainingLoadRatio")
        if ati is not None or cti is not None:
            atl = float(ati) if ati is not None else None
            ctl = float(cti) if cti is not None else None
            if atl is not None and ctl is not None:
                tsb = round(ctl - atl, 1)
            print(f"COROS training load (dayDetail): ATL={atl} CTL={ctl} TSB={tsb} ratio={tl_ratio}")
            break

    # Fallback: analyse endpoint
    if atl is None and ctl is None:
        today = date.today()
        params = {
            "startDay": (today - timedelta(days=7)).strftime("%Y%m%d"),
            "endDay": today.strftime("%Y%m%d"),
        }
        data = await _get(db_user_id, "/analyse/training/load/query", params)
        if _ok(data):
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
            print(f"COROS training load (analyse): CTL={ctl} ATL={atl} TSB={tsb}")

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
    """HRV из dayDetail — ночной RMSSD и среднее за неделю."""
    items = await _get_day_detail(db_user_id, days=7)
    hrv_values = []
    for item in items:
        for key in ("avg_sleep_hrv", "avgSleepHrv", "hrv", "hrvValue", "avgHrv", "nightHrv", "rmssd"):
            v = item.get(key)
            if v and float(v) > 0:
                hrv_values.append(float(v))
                break
    if not hrv_values:
        return None
    hrv_last = hrv_values[-1]
    hrv_avg = round(sum(hrv_values) / len(hrv_values), 1)
    print(f"COROS HRV (dayDetail): last={hrv_last} avg={hrv_avg} ({len(hrv_values)} days)")
    return {
        "hrv_last_night": hrv_last,
        "hrv_weekly_avg": hrv_avg,
        "status": "normal",
    }


async def get_recovery(db_user_id: int) -> dict | None:
    """Восстановление из dayDetail — tired_rate (0–100%) → Recovery Score.

    tired_rate — нативная метрика COROS «уровень усталости» (0=свежий, 100=выжат).
    Recovery Score = 100 - tired_rate (аналог скриншота из приложения).
    """
    items = await _get_day_detail(db_user_id, days=3)
    if not items:
        return None

    latest = items[-1]

    # tired_rate — основной путь (нативный Recovery % из приложения)
    tired_rate = latest.get("tired_rate") or latest.get("tiredRate") or latest.get("fatigueRate")
    # rhr — ЧСС покоя
    rhr = latest.get("rhr") or latest.get("restingHr") or latest.get("restingHeartRate")

    score = None
    if tired_rate is not None and float(tired_rate) >= 0:
        score = max(0, min(100, round(100 - float(tired_rate))))
    
    print(f"COROS recovery (dayDetail): score={score} tired_rate={tired_rate} rhr={rhr}")
    if score is None and rhr is None:
        return None

    return {
        "source": "coros",
        "recovery_score": score,
        "rhr": int(rhr) if rhr else None,
    }


async def get_full_data(db_user_id: int) -> dict | None:
    """Все данные для промта: VO2max, нагрузка, HRV, готовность.

    Возвращает dict совместимый со структурой fitness/recovery для промта,
    или None если COROS не подключён или не отвечает.
    """
    if not _load_token(db_user_id):
        return None

    # Основной источник — /dashboard/query (рабочий endpoint)
    dashboard, training_load = await asyncio.gather(
        get_dashboard_data(db_user_id),
        get_training_load(db_user_id),
        return_exceptions=True,
    )

    if isinstance(dashboard, Exception):
        dashboard = None
    if isinstance(training_load, Exception):
        training_load = None

    # Нет ни dashboard ни нагрузки — не возвращаем ничего
    if not dashboard and not training_load:
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

    if dashboard:
        # VO2max
        if dashboard.get("vo2max"):
            fitness["vo2max"] = dashboard["vo2max"]
            fitness["vo2max_source"] = "COROS"
        # Лактатный порог — напрямую в профиль
        if dashboard.get("lactate_threshold_pace"):
            fitness["lactate_threshold_pace"] = dashboard["lactate_threshold_pace"]
        if dashboard.get("lactate_threshold_hr"):
            fitness["lactate_threshold_hr"] = dashboard["lactate_threshold_hr"]
        # Восстановление
        recovery_dict: dict = {"source": "coros"}
        if dashboard.get("recovery_score") is not None:
            recovery_dict["recovery_score"] = dashboard["recovery_score"]
        if dashboard.get("recovery_state") is not None:
            recovery_dict["recovery_state"] = dashboard["recovery_state"]
        if dashboard.get("full_recovery_hours") is not None:
            recovery_dict["full_recovery_hours"] = dashboard["full_recovery_hours"]
        if dashboard.get("hrv") is not None:
            recovery_dict["hrv"] = dashboard["hrv"]
        if dashboard.get("hrv_baseline") is not None:
            recovery_dict["hrv_baseline"] = dashboard["hrv_baseline"]
        if dashboard.get("hrv_weekly_avg") is not None:
            recovery_dict["hrv_weekly_avg"] = dashboard["hrv_weekly_avg"]
        if dashboard.get("rhr") is not None:
            recovery_dict["rhr"] = dashboard["rhr"]
        if len(recovery_dict) > 1:
            fitness["recovery"] = recovery_dict
        if dashboard.get("recovery_score") is not None:
            score = dashboard["recovery_score"]
            fitness["fatigue_level"] = (
                "fresh" if score >= 70 else "tired" if score < 40 else "normal"
            )

    if training_load:
        fitness["training_load"] = training_load
        tsb = training_load.get("tsb")
        if tsb is not None and fitness["fatigue_level"] == "unknown":
            fitness["fatigue_level"] = (
                "fresh" if tsb > 5 else "tired" if tsb < -15 else "normal"
            )

    # Собираем summary
    parts = []
    if fitness.get("vo2max"):
        parts.append(f"VO2max {fitness['vo2max']}")
    if fitness.get("lactate_threshold_pace"):
        parts.append(f"ЛП {fitness['lactate_threshold_pace']} мин/км")
    if training_load and training_load.get("summary"):
        parts.append(training_load["summary"])
    fitness["summary"] = " | ".join(parts) if parts else "COROS данные получены"

    return fitness


# ── Для recovery блока (совместимость с _get_recovery_data) ───

async def get_recovery_for_prompt(db_user_id: int) -> dict | None:
    """Возвращает recovery dict в формате совместимом с Whoop/Garmin.
    Используется в _get_recovery_data() как fallback после Garmin.

    Данные берутся из одного вызова dayDetail — hrv и recovery из одного источника.
    """
    if not _load_token(db_user_id):
        return None

    # Основной источник — /dashboard/query
    dashboard = await get_dashboard_data(db_user_id)

    result: dict = {"source": "coros"}

    if dashboard:
        for key in ("recovery_score", "recovery_state", "full_recovery_hours",
                    "hrv", "hrv_baseline", "hrv_weekly_avg", "rhr"):
            if dashboard.get(key) is not None:
                result[key] = dashboard[key]

    if len(result) <= 1:  # только "source"
        return None

    return result
