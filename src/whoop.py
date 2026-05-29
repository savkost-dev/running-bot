import aiohttp
import asyncio
import os
import time as _time
from dotenv import load_dotenv

load_dotenv()

WHOOP_CLIENT_ID = os.getenv("WHOOP_CLIENT_ID")
WHOOP_CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET")
WHOOP_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_API_BASE = "https://api.prod.whoop.com/developer/v2"

WHOOP_REDIRECT_URI = "https://httpbin.org/get"

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_MAX_RETRIES = 3


def get_auth_url(telegram_id: int) -> str:
    import urllib.parse
    params = urllib.parse.urlencode({
        "client_id": WHOOP_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": WHOOP_REDIRECT_URI,
        "scope": "offline read:recovery read:sleep read:profile read:cycles",
        "state": str(telegram_id),
    })
    return f"{WHOOP_AUTH_URL}?{params}"


async def _api_get(session: aiohttp.ClientSession, url: str, headers: dict, params: dict = None) -> dict | None:
    """GET с таймаутом и повторами."""
    for attempt in range(_MAX_RETRIES):
        try:
            async with session.get(url, headers=headers, params=params, timeout=_TIMEOUT) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status in (401, 403):
                    print(f"Whoop auth error {resp.status} on {url}")
                    return None
                body = await resp.text()
                print(f"Whoop {url} status={resp.status} attempt={attempt+1}: {body[:200]}")
        except asyncio.TimeoutError:
            print(f"Whoop timeout on {url}, attempt={attempt+1}")
        except Exception as e:
            print(f"Whoop error on {url}: {e}, attempt={attempt+1}")
        if attempt < _MAX_RETRIES - 1:
            await asyncio.sleep(2)
    return None


async def exchange_code(code: str) -> dict:
    """Обменивает code на access/refresh токены"""
    async with aiohttp.ClientSession() as session:
        async with session.post(WHOOP_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": WHOOP_REDIRECT_URI,
            "client_id": WHOOP_CLIENT_ID,
            "client_secret": WHOOP_CLIENT_SECRET,
        }, timeout=_TIMEOUT) as resp:
            return await resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    """Обновляет просроченный access token"""
    async with aiohttp.ClientSession() as session:
        async with session.post(WHOOP_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": WHOOP_CLIENT_ID,
            "client_secret": WHOOP_CLIENT_SECRET,
        }, timeout=_TIMEOUT) as resp:
            return await resp.json()


async def get_recovery(access_token: str) -> dict | None:
    """Получает данные восстановления за последнюю ночь."""
    headers = {"Authorization": f"Bearer {access_token}"}

    async with aiohttp.ClientSession() as session:
        data = await _api_get(
            session,
            f"{WHOOP_API_BASE}/recovery",
            headers,
            params={"limit": 1},
        )

    if not data:
        return None

    records = data.get("records", [])
    if not records:
        print("Whoop recovery: нет записей")
        return None

    r = records[0]
    score = r.get("score") or {}
    recovery_score = score.get("recovery_score")
    date = r.get("created_at", "")[:10]
    print(f"Whoop recovery: score={recovery_score}, date={date}")

    return {
        "recovery_score": recovery_score,
        "hrv": round(score.get("hrv_rmssd_milli") or 0, 1),
        "resting_hr": score.get("resting_heart_rate"),
        "date": date,
    }


async def get_sleep(access_token: str) -> dict | None:
    """Получает данные последнего сна."""
    headers = {"Authorization": f"Bearer {access_token}"}

    async with aiohttp.ClientSession() as session:
        data = await _api_get(
            session,
            f"{WHOOP_API_BASE}/activity/sleep",
            headers,
            params={"limit": 1},
        )

    if not data:
        return None

    records = data.get("records", [])
    if not records:
        return None

    s = records[0]
    score = s.get("score") or {}
    stage_summary = score.get("stage_summary") or {}
    total_sleep_ms = (
        stage_summary.get("total_light_sleep_time_milli", 0) +
        stage_summary.get("total_slow_wave_sleep_time_milli", 0) +
        stage_summary.get("total_rem_sleep_time_milli", 0)
    )

    return {
        "sleep_score": score.get("sleep_performance_percentage"),
        "sleep_hours": round(total_sleep_ms / 3_600_000, 1) if total_sleep_ms else None,
        "sleep_efficiency": score.get("sleep_efficiency_percentage"),
        "date": s.get("start", "")[:10],
    }


async def get_full_recovery_data(access_token: str) -> dict | None:
    """
    Объединяет данные recovery + sleep в один словарь
    для передачи в claude_advisor.
    """
    recovery, sleep = await asyncio.gather(
        get_recovery(access_token),
        get_sleep(access_token),
        return_exceptions=False,
    )

    if not recovery and not sleep:
        return None

    result = {}

    if recovery:
        result["recovery_score"] = recovery.get("recovery_score")
        result["hrv"] = recovery.get("hrv")
        result["resting_hr"] = recovery.get("resting_hr")

    if sleep:
        result["sleep_score"] = sleep.get("sleep_score")
        result["sleep_hours"] = sleep.get("sleep_hours")
        result["sleep_efficiency"] = sleep.get("sleep_efficiency")

    result["body_battery"] = None  # Garmin — добавим позже
    result["source"] = "whoop"

    return result


async def ensure_valid_token(db_user_id: int) -> str | None:
    """
    Проверяет токен и обновляет если нужно.
    Возвращает актуальный access_token или None.
    """
    from database import get_token, save_token

    token_data = get_token(db_user_id, "whoop")
    if not token_data:
        return None

    access_token = token_data["access_token"]
    expires_at = token_data.get("expires_at")

    # Проверяем не истёк ли токен (expires_at хранится как unix timestamp)
    needs_refresh = False
    if expires_at:
        try:
            exp_time = int(expires_at)
            # Если значение маленькое (< 10 лет в секундах), это относительное время — обновляем
            if exp_time < 315_360_000 or exp_time < _time.time() + 300:
                needs_refresh = True
        except (ValueError, TypeError):
            needs_refresh = True
    else:
        needs_refresh = True

    if needs_refresh:
        print(f"Whoop: обновляем токен для user_id={db_user_id}")
        try:
            new_tokens = await refresh_access_token(token_data["refresh_token"])
        except Exception as e:
            print(f"Whoop refresh error: {e}")
            return access_token  # возвращаем старый, вдруг ещё работает

        if "access_token" in new_tokens:
            new_expires_at = str(int(_time.time()) + int(new_tokens.get("expires_in", 3600)))
            save_token(
                db_user_id, "whoop",
                new_tokens["access_token"],
                new_tokens.get("refresh_token", token_data["refresh_token"]),
                new_expires_at,
            )
            access_token = new_tokens["access_token"]
            print(f"Whoop: токен обновлён, expires_at={new_expires_at}")
        else:
            print(f"Whoop refresh failed: {new_tokens}")

    return access_token


if __name__ == "__main__":
    print("Whoop auth URL (для теста):")
    print(get_auth_url(123456789))
