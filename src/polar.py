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
    polar_user_id = _load_polar_user_id(db_user_id)
    if not polar_user_id:
        return None

    today = date.today()
    params = {
        "from": (today - timedelta(days=7)).isoformat(),
        "to":   today.isoformat(),
    }
    data = await _get(db_user_id, f"/users/{polar_user_id}/nightly-recharge", params)
    if not data:
        return None

    # API может вернуть list или {"recharges": [...]} или {"items": [...]}
    items = (
        data if isinstance(data, list)
        else data.get("recharges") or data.get("items") or []
    )
    if not items:
        return None

    last = items[-1] if isinstance(items[-1], dict) else {}

    ans_charge = last.get("ans-charge") or last.get("ansCharge")
    ans_rate   = last.get("ans-rate")   or last.get("ansRate")
    hrv_rmssd  = last.get("hrv-rmssd") or last.get("hrvRmssd")
    hrv_avg    = last.get("hrv-avg")    or last.get("hrvAvg")
    hr_avg     = last.get("heart-rate-avg") or last.get("heartRateAvg")

    hrv = hrv_rmssd or hrv_avg  # RMSSD ближе к метрике Garmin/Whoop

    if ans_charge is None and hrv is None:
        return None

    print(f"Polar nightly recharge: ans={ans_charge} ({ans_rate}), hrv={hrv}")
    return {
        "source": "polar",
        "recovery_score": int(float(ans_charge)) if ans_charge is not None else None,
        "hrv": round(float(hrv), 1) if hrv is not None else None,
        "hr_avg": int(float(hr_avg)) if hr_avg is not None else None,
        "ans_rate": str(ans_rate) if ans_rate else None,
    }


async def get_sleep(db_user_id: int) -> dict | None:
    """Данные сна за последние 7 дней."""
    polar_user_id = _load_polar_user_id(db_user_id)
    if not polar_user_id:
        return None

    today = date.today()
    params = {
        "from": (today - timedelta(days=7)).isoformat(),
        "to":   today.isoformat(),
    }
    data = await _get(db_user_id, f"/users/{polar_user_id}/sleep", params)
    if not data:
        return None

    items = (
        data if isinstance(data, list)
        else data.get("nights") or data.get("items") or []
    )
    if not items:
        return None

    last = items[-1] if isinstance(items[-1], dict) else {}

    sleep_score    = last.get("sleep-score")  or last.get("sleepScore")
    total_sleep_s  = last.get("total-sleep")  or last.get("totalSleep") or 0
    total_sleep_h  = round(int(total_sleep_s) / 3600, 1) if total_sleep_s else None

    print(f"Polar sleep: score={sleep_score}, hours={total_sleep_h}")
    return {
        "sleep_score": int(sleep_score) if sleep_score is not None else None,
        "sleep_hours": total_sleep_h,
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
        fitness["recovery"] = recharge
        ans = recharge.get("recovery_score")
        if ans is not None:
            fitness["fatigue_level"] = (
                "fresh"  if ans >= 70 else
                "tired"  if ans <  40 else
                "normal"
            )

    if sleep:
        fitness["sleep"] = sleep

    parts = []
    if vo2max:
        parts.append(f"VO2max {vo2max} (Polar)")
    if recharge and recharge.get("recovery_score") is not None:
        rate = recharge.get("ans_rate") or ""
        parts.append(f"ANS Recharge {recharge['recovery_score']}% ({rate})")
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
        if recharge.get("recovery_score") is not None:
            result["recovery_score"] = recharge["recovery_score"]
        if recharge.get("hrv") is not None:
            result["hrv"] = recharge["hrv"]

    if not isinstance(sleep, Exception) and sleep:
        if sleep.get("sleep_hours") is not None:
            result["sleep_hours"] = sleep["sleep_hours"]
        if sleep.get("sleep_score") is not None:
            result["sleep_score"] = sleep["sleep_score"]

    if len(result) <= 1:  # только "source"
        return None

    return result
