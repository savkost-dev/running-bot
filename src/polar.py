"""
Polar AccessLink API v3 интеграция — OAuth 2.0.

Авторизация: https://flow.polar.com/oauth2/authorization → code
Токены:      POST https://polarremote.com/v2/oauth2/token  (Basic Auth)
API:         https://www.polaraccesslink.com/v3

Документация: https://www.polaraccesslink.com/v3
"""
import os
import base64
import asyncio
import time as _time
import urllib.parse
from datetime import date, timedelta

import aiohttp
from dotenv import load_dotenv

load_dotenv()

POLAR_CLIENT_ID     = os.getenv("POLAR_CLIENT_ID", "")
POLAR_CLIENT_SECRET = os.getenv("POLAR_CLIENT_SECRET", "")
_REDIRECT_BASE = os.getenv("OAUTH_REDIRECT_BASE", "http://167.172.185.88:8080")
_REDIRECT_URI  = f"{_REDIRECT_BASE}/polar/callback"

_AUTH_URL  = "https://flow.polar.com/oauth2/authorization"
_TOKEN_URL = "https://polarremote.com/v2/oauth2/token"
_BASE_URL  = "https://www.polaraccesslink.com/v3"
_SERVICE   = "polar"
_TIMEOUT   = aiohttp.ClientTimeout(total=30)


# ── Утилиты ──────────────────────────────────────────────────

def _basic_auth() -> str:
    """Base64-кодированный 'client_id:client_secret' для HTTP Basic Auth."""
    return base64.b64encode(f"{POLAR_CLIENT_ID}:{POLAR_CLIENT_SECRET}".encode()).decode()


def _load_token(db_user_id: int) -> str | None:
    from database import get_token
    data = get_token(db_user_id, _SERVICE)
    return data["access_token"] if data else None


def _load_refresh_token(db_user_id: int) -> str | None:
    from database import get_token
    data = get_token(db_user_id, _SERVICE)
    return data["refresh_token"] if data else None


def _save_tokens(db_user_id: int, access_token: str, refresh_token: str,
                 expires_in: int = 21600) -> None:
    from database import save_token
    expires_at = str(int(_time.time()) + expires_in)
    save_token(db_user_id, _SERVICE, access_token, refresh_token, expires_at)


def _load_polar_user_id(db_user_id: int) -> str | None:
    from database import get_user_profile
    p = get_user_profile(db_user_id)
    return (p or {}).get("polar_user_id")


def _save_polar_user_id(db_user_id: int, polar_user_id: str) -> None:
    from database import save_user_profile
    save_user_profile(db_user_id, polar_user_id=str(polar_user_id))


def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ── OAuth ─────────────────────────────────────────────────────

def get_auth_url(telegram_id: int) -> str:
    """Формирует URL авторизации Polar Flow."""
    if not POLAR_CLIENT_ID:
        return ""
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": POLAR_CLIENT_ID,
        "redirect_uri": _REDIRECT_URI,
        "scope": "accesslink.read_all",
        "state": str(telegram_id),
    })
    return f"{_AUTH_URL}?{params}"


async def exchange_code(code: str) -> dict:
    """Обменивает authorization code на access/refresh токены.

    Возвращает dict с ключами: access_token, refresh_token, expires_in, x_user_id
    """
    headers = {
        "Authorization": f"Basic {_basic_auth()}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _REDIRECT_URI,
    }
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(_TOKEN_URL, data=data, headers=headers) as resp:
            return await resp.json(content_type=None)


async def register_user(access_token: str, db_user_id: int,
                        x_user_id: int | None = None) -> str | None:
    """Регистрирует пользователя в Polar AccessLink, возвращает polar_user_id.

    201 → новый пользователь, 409 → уже зарегистрирован.
    В обоих случаях тело ответа содержит polar-user-id.
    Если API недоступен — возвращаем x_user_id из токена как fallback.
    """
    headers = _headers(access_token)
    payload = {"member-id": str(db_user_id)}
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(
            f"{_BASE_URL}/users", json=payload, headers=headers
        ) as resp:
            if resp.status in (200, 201, 409):
                try:
                    body = await resp.json(content_type=None)
                    pid = (
                        body.get("polar-user-id")
                        or body.get("polarUserId")
                        or body.get("id")
                    )
                    if pid:
                        print(f"Polar register_user: polar_user_id={pid} (HTTP {resp.status})")
                        return str(pid)
                except Exception as e:
                    print(f"Polar register_user parse error: {e}")
            else:
                text = await resp.text()
                print(f"Polar register_user HTTP {resp.status}: {text[:200]}")

    # Fallback: если регистрация не вернула ID — используем x_user_id из токена
    if x_user_id:
        print(f"Polar register_user fallback: using x_user_id={x_user_id}")
        return str(x_user_id)
    return None


async def refresh_access_token(db_user_id: int) -> str | None:
    """Обновляет access_token по refresh_token."""
    refresh_token = _load_refresh_token(db_user_id)
    if not refresh_token:
        return None
    headers = {
        "Authorization": f"Basic {_basic_auth()}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(_TOKEN_URL, data=data, headers=headers) as resp:
                body = await resp.json(content_type=None)
                new_access = body.get("access_token")
                new_refresh = body.get("refresh_token", refresh_token)
                expires_in = body.get("expires_in", 21600)
                if new_access:
                    _save_tokens(db_user_id, new_access, new_refresh, expires_in)
                    return new_access
    except Exception as e:
        print(f"Polar refresh_access_token error: {e}")
    return None


# ── HTTP хелпер ───────────────────────────────────────────────

async def _get(db_user_id: int, path: str, params: dict | None = None,
               _retry: bool = True) -> dict | list | None:
    """GET к Polar API с автоматическим обновлением токена при 401."""
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
                    new_token = await refresh_access_token(db_user_id)
                    if new_token:
                        return await _get(db_user_id, path, params, _retry=False)
                    return None
                if resp.status == 204:
                    return {}
                if resp.status not in (200, 201):
                    text = await resp.text()
                    print(f"Polar GET {path} → HTTP {resp.status}: {text[:200]}")
                    return None
                return await resp.json(content_type=None)
    except Exception as e:
        print(f"Polar GET {path} error: {e}")
        return None


# ── Данные ───────────────────────────────────────────────────

async def get_nightly_recharge(db_user_id: int) -> dict | None:
    """Ночное восстановление (ANS recharge + HRV) за последние 7 дней.

    ANS charge: 0-100 — аналог Whoop Recovery Score.
    """
    today = date.today()
    params = {
        "from": (today - timedelta(days=7)).isoformat(),
        "to":   today.isoformat(),
    }
    # Правильный путь — без userId (токен сам определяет пользователя)
    data = await _get(db_user_id, "/users/nightly-recharge", params)
    if not data:
        return None

    items = (
        data if isinstance(data, list)
        else data.get("recharges") or data.get("items") or []
    )
    if not items:
        return None

    # Берём самую свежую запись по дате
    items = [x for x in items if isinstance(x, dict)]
    items.sort(key=lambda x: x.get("date", ""))
    last = items[-1] if items else {}

    # Реальные поля Polar v3 — через подчёркивание
    ans_charge   = last.get("ans_charge")
    ans_status   = last.get("ans_charge_status")
    night_status = last.get("nightly_recharge_status")
    hrv          = last.get("heart_rate_variability_avg")
    hr_avg       = last.get("heart_rate_avg")
    br_avg       = last.get("breathing_rate_avg")

    if ans_charge is None and hrv is None:
        return None

    print(f"Polar nightly recharge [{last.get('date')}]: "
          f"ans_charge={ans_charge} (status {ans_status}), hrv={hrv}, hr={hr_avg}")
    return {
        "source": "polar",
        "date": last.get("date"),
        "ans_charge": float(ans_charge) if ans_charge is not None else None,
        "ans_charge_status": int(ans_status) if ans_status is not None else None,
        "nightly_recharge_status": int(night_status) if night_status is not None else None,
        "hrv": round(float(hrv), 1) if hrv is not None else None,
        "hr_avg": int(float(hr_avg)) if hr_avg is not None else None,
        "breathing_rate": round(float(br_avg), 1) if br_avg is not None else None,
    }


async def get_sleep(db_user_id: int) -> dict | None:
    """Данные сна за последние 7 дней."""
    today = date.today()
    params = {
        "from": (today - timedelta(days=7)).isoformat(),
        "to":   today.isoformat(),
    }
    # Правильный путь — без userId
    data = await _get(db_user_id, "/users/sleep", params)
    if not data:
        return None

    items = (
        data if isinstance(data, list)
        else data.get("nights") or data.get("items") or []
    )
    items = [x for x in items if isinstance(x, dict)]
    if not items:
        return None

    items.sort(key=lambda x: x.get("date", ""))
    last = items[-1]

    sleep_score = last.get("sleep_score")
    # Общее время сна = фазы light + deep + rem (в секундах)
    light = last.get("light_sleep", 0) or 0
    deep  = last.get("deep_sleep", 0) or 0
    rem   = last.get("rem_sleep", 0) or 0
    total_s = light + deep + rem
    total_h = round(total_s / 3600, 1) if total_s else None

    print(f"Polar sleep [{last.get('date')}]: score={sleep_score}, hours={total_h}")
    return {
        "date": last.get("date"),
        "sleep_score": int(sleep_score) if sleep_score is not None else None,
        "sleep_hours": total_h,
        "sleep_charge": last.get("sleep_charge"),
        "sleep_cycles": last.get("sleep_cycles"),
        "deep_sleep_min": round(deep / 60) if deep else None,
        "rem_sleep_min": round(rem / 60) if rem else None,
    }


async def get_vo2max(db_user_id: int) -> float | None:
    """VO2max из профиля пользователя Polar (если доступно)."""
    polar_user_id = _load_polar_user_id(db_user_id)
    if not polar_user_id:
        return None

    data = await _get(db_user_id, f"/users/{polar_user_id}")
    if not isinstance(data, dict):
        return None

    # Polar может отдавать aerobic-fitness или vo2max напрямую
    for key in ("vo2max", "vo2Max", "aerobic-fitness", "aerobicFitness"):
        val = data.get(key)
        if val is not None:
            try:
                fval = float(val)
                if fval > 10:
                    print(f"Polar vo2max={fval} (key={key})")
                    return round(fval, 1)
            except (TypeError, ValueError):
                pass

    return None


async def get_profile(db_user_id: int) -> dict | None:
    """Сырые профильные показатели Polar as is (слой 1.1).

    Грузит пол и дату рождения как есть, без обработки.
    Возраст из birthdate считается позже (слой 1.2/2).
    """
    polar_user_id = _load_polar_user_id(db_user_id)
    if not polar_user_id:
        return None

    data = await _get(db_user_id, f"/users/{polar_user_id}")
    if not isinstance(data, dict):
        return None

    result: dict = {"source": "polar"}
    if data.get("gender") is not None:
        result["gender"] = data["gender"]        # MALE / FEMALE — as is
    if data.get("birthdate") is not None:
        result["birthdate"] = data["birthdate"]  # "1982-08-20" — as is

    return result if len(result) > 1 else None


async def get_full_data(db_user_id: int) -> dict | None:
    """Все данные для промта: ANS recharge, HRV, сон, VO2max.

    Polar силён в данных восстановления (ANS recharge, HRV от ночного сна).
    Training load не предоставляет — возвращаем recovery-ориентированный fitness dict.
    """
    if not _load_token(db_user_id) or not _load_polar_user_id(db_user_id):
        return None

    recharge, sleep, vo2max = await asyncio.gather(
        get_nightly_recharge(db_user_id),
        get_sleep(db_user_id),
        get_vo2max(db_user_id),
        return_exceptions=True,
    )

    if isinstance(recharge, Exception): recharge = None
    if isinstance(sleep,    Exception): sleep    = None
    if isinstance(vo2max,   Exception): vo2max   = None

    if not recharge and not vo2max:
        print(f"Polar get_full_data: нет данных для user_id={db_user_id}")
        return None

    fitness: dict = {
        "source": "polar",
        "summary": "",
        "total_km": 0,
        "run_count": 0,
        "avg_pace": "—",
        "avg_hr": None,
        "fatigue_level": "unknown",
    }

    if vo2max:
        fitness["vo2max"]        = vo2max
        fitness["vo2max_source"] = "Polar"

    if recharge:
        # ANS charge (-10..+10) → recovery_score 0–100
        ans_charge = recharge.get("ans_charge")
        rec_score = None
        if ans_charge is not None:
            rec_score = max(0, min(100, round((ans_charge + 10) / 20 * 100)))
            recharge["recovery_score"] = rec_score
        fitness["recovery"] = recharge
        if rec_score is not None:
            fitness["fatigue_level"] = (
                "fresh"  if rec_score >= 70 else
                "tired"  if rec_score <  40 else
                "normal"
            )

    if sleep:
        fitness["sleep"] = sleep

    parts = []
    if vo2max:
        parts.append(f"VO2max {vo2max} (Polar)")
    if recharge and recharge.get("recovery_score") is not None:
        parts.append(f"ANS Recharge {recharge['recovery_score']}%")
    fitness["summary"] = " | ".join(parts) if parts else "Polar данные получены"

    return fitness


async def get_recovery_for_prompt(db_user_id: int) -> dict | None:
    """Recovery dict совместимый с Whoop/Garmin/COROS для _get_recovery_data()."""
    if not _load_token(db_user_id) or not _load_polar_user_id(db_user_id):
        return None

    recharge, sleep = await asyncio.gather(
        get_nightly_recharge(db_user_id),
        get_sleep(db_user_id),
        return_exceptions=True,
    )

    result: dict = {"source": "polar"}

    if not isinstance(recharge, Exception) and recharge:
        # ANS charge (-10..+10) → recovery_score 0–100
        ans_charge = recharge.get("ans_charge")
        if ans_charge is not None:
            result["recovery_score"] = max(0, min(100, round((ans_charge + 10) / 20 * 100)))
        if recharge.get("hrv") is not None:
            result["hrv"] = recharge["hrv"]
        if recharge.get("hr_avg") is not None:
            result["rhr"] = recharge["hr_avg"]

    if not isinstance(sleep, Exception) and sleep:
        if sleep.get("sleep_hours") is not None:
            result["sleep_hours"] = sleep["sleep_hours"]
        if sleep.get("sleep_score") is not None:
            result["sleep_score"] = sleep["sleep_score"]

    if len(result) <= 1:  # только "source"
        return None

    return result

# ── Слой 1.1: загрузка сырых данных ──────────────────────────

async def fetch_raw(db_user_id: int) -> dict | None:
    """Слой 1.1: тянет сырые ответы Polar и сохраняет в raw_service_data as is.

    Собирает три endpoint в один объект:
    - profile          — /users/{id} (пол, дата рождения, антропометрия)
    - nightly_recharge — /users/nightly-recharge (восстановление, HRV)
    - sleep            — /users/sleep (сон)

    Ничего не парсит — кладёт сырые JSON как пришли. Парсинг — слой 2.
    """
    import json
    import database as db

    polar_user_id = _load_polar_user_id(db_user_id)
    if not polar_user_id:
        return None

    today = date.today()
    frm = (today - timedelta(days=7)).isoformat()
    to = today.isoformat()

    profile, recharge, sleep = await asyncio.gather(
        _get(db_user_id, f"/users/{polar_user_id}"),
        _get(db_user_id, "/users/nightly-recharge", {"from": frm, "to": to}),
        _get(db_user_id, "/users/sleep", {"from": frm, "to": to}),
        return_exceptions=True,
    )

    if isinstance(profile,  Exception): profile  = None
    if isinstance(recharge, Exception): recharge = None
    if isinstance(sleep,    Exception): sleep    = None

    if not profile and not recharge and not sleep:
        print(f"Polar fetch_raw: нет данных для user_id={db_user_id}")
        return None

    raw = {
        "profile": profile,
        "nightly_recharge": recharge,
        "sleep": sleep,
    }

    db.save_raw_service_data(db_user_id, _SERVICE, json.dumps(raw, ensure_ascii=False))
    print(f"Polar fetch_raw: сохранено для user_id={db_user_id} "
          f"(profile={bool(profile)}, recharge={bool(recharge)}, sleep={bool(sleep)})")
    return raw
