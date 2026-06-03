import aiohttp
import os
import math
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"

# Стандартные дистанции для прогнозов
RACE_DISTANCES = {
    "400m": 400,
    "1km": 1000,
    "1 mile": 1609,
    "5km": 5000,
    "10km": 10000,
    "Half-Marathon": 21097,
    "Marathon": 42195,
}


OAUTH_REDIRECT_BASE = "http://167.172.185.88:8080"


def get_auth_url(telegram_id: int) -> str:
    params = (
        f"client_id={STRAVA_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={OAUTH_REDIRECT_BASE}/strava/callback"
        f"&approval_prompt=auto"
        f"&scope=read,activity:read_all"
        f"&state={telegram_id}"
    )
    return f"{STRAVA_AUTH_URL}?{params}"


async def exchange_code(code: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(STRAVA_TOKEN_URL, data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code"
        }) as resp:
            return await resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(STRAVA_TOKEN_URL, data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }) as resp:
            return await resp.json()


async def ensure_valid_token(db_user_id: int) -> str | None:
    """Проверяет токен и обновляет если нужно"""
    import time
    from database import get_token, save_token

    token_data = get_token(db_user_id, "strava")
    if not token_data:
        return None

    expires_at = token_data.get("expires_at")
    if expires_at:
        try:
            if int(expires_at) < time.time() + 300:
                new = await refresh_access_token(token_data["refresh_token"])
                if "access_token" in new:
                    save_token(db_user_id, "strava",
                        new["access_token"],
                        new["refresh_token"],
                        str(new["expires_at"])
                    )
                    return new["access_token"]
        except (ValueError, TypeError):
            pass

    return token_data["access_token"]


async def get_recent_activities(access_token: str, days: int = 90) -> list:
    """Получает все активности за последние N дней"""
    after = int((datetime.now() - timedelta(days=days)).timestamp())
    headers = {"Authorization": f"Bearer {access_token}"}
    activities = []

    async with aiohttp.ClientSession() as session:
        page = 1
        while True:
            async with session.get(
                f"{STRAVA_API_BASE}/athlete/activities",
                headers=headers,
                params={"after": after, "per_page": 100, "page": page}
            ) as resp:
                batch = await resp.json()
            if not batch:
                break
            activities.extend(batch)
            if len(batch) < 100:
                break
            page += 1

    return activities


async def get_activity_detail(access_token: str, activity_id: int) -> dict | None:
    """Получает детали активности включая best_efforts"""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{STRAVA_API_BASE}/activities/{activity_id}",
            headers=headers
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json()


async def get_recent_48h_load(access_token: str) -> dict:
    """Острая нагрузка за последние 48 часов — всегда свежие данные (1 запрос к API)."""
    after = int((datetime.now() - timedelta(hours=48)).timestamp())
    headers = {"Authorization": f"Bearer {access_token}"}
    activities = []

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers=headers,
            params={"after": after, "per_page": 30, "page": 1}
        ) as resp:
            if resp.status == 200:
                activities = await resp.json()

    runs = [a for a in activities if a.get("type") == "Run"]
    if not runs:
        return {"total_km_48h": 0.0, "sessions_48h": 0, "suffer_48h": 0,
                "last_activity_hours_ago": None}

    total_km = round(sum(a.get("distance", 0) for a in runs) / 1000, 1)
    suffer   = sum(a.get("suffer_score") or 0 for a in runs)

    # Сколько часов назад последняя активность
    last_date_str = runs[0].get("start_date", "")
    hours_ago = None
    if last_date_str:
        try:
            last_dt = datetime.strptime(last_date_str[:19], "%Y-%m-%dT%H:%M:%S")
            hours_ago = round((datetime.now() - last_dt).total_seconds() / 3600, 1)
        except Exception:
            pass

    return {
        "total_km_48h":          total_km,
        "sessions_48h":          len(runs),
        "suffer_48h":            suffer,
        "last_activity_hours_ago": hours_ago,
    }


async def get_recent_runs(access_token: str, days: int = 14) -> list:
    """Получает пробежки за последние N дней (для анализа усталости)"""
    activities = await get_recent_activities(access_token, days)
    runs = []
    for a in activities:
        if a.get("type") != "Run":
            continue
        runs.append({
            "date": a.get("start_date_local", "")[:10],
            "name": a.get("name", ""),
            "distance_km": round(a.get("distance", 0) / 1000, 2),
            "duration_min": round(a.get("moving_time", 0) / 60, 1),
            "avg_pace": _calc_pace(a.get("distance", 0), a.get("moving_time", 0)),
            "avg_hr": a.get("average_heartrate"),
            "suffer_score": a.get("suffer_score"),
            "elevation_m": round(a.get("total_elevation_gain", 0), 1),
            "is_race": a.get("workout_type") == 1,
        })
    return runs


async def get_training_load(access_token: str) -> dict:
    """
    Считает CTL/ATL/TSB (Fitness/Fatigue/Form) на основе suffer_score.
    Только активности с пульсом (suffer_score > 0).

    CTL (Fitness) = 42-дневная экспоненциальная средняя
    ATL (Fatigue) = 7-дневная экспоненциальная средняя
    TSB (Form)    = CTL - ATL
    """
    activities = await get_recent_activities(access_token, days=90)

    # Фильтруем только пробежки с пульсом
    runs_with_hr = [
        a for a in activities
        if a.get("type") == "Run"
        and a.get("suffer_score") is not None
        and a.get("suffer_score", 0) > 0
    ]

    if not runs_with_hr:
        return {
            "ctl": None, "atl": None, "tsb": None,
            "trend": None, "summary": "Нет данных о нагрузке (нужен пульс на тренировках)"
        }

    # Группируем нагрузку по дням
    daily_load = {}
    for a in runs_with_hr:
        date = a.get("start_date_local", "")[:10]
        if date:
            daily_load[date] = daily_load.get(date, 0) + (a.get("suffer_score") or 0)

    # Считаем CTL и ATL методом экспоненциального сглаживания
    ctl_k = 2 / (42 + 1)  # константа для 42 дней
    atl_k = 2 / (7 + 1)   # константа для 7 дней

    ctl = 0.0
    atl = 0.0
    today = datetime.now().date()

    # Проходим по всем дням за последние 90 дней
    for i in range(89, -1, -1):
        date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        load = daily_load.get(date, 0)
        ctl = ctl + ctl_k * (load - ctl)
        atl = atl + atl_k * (load - atl)

    tsb = ctl - atl

    # Тренд: сравниваем CTL сейчас vs 30 дней назад
    ctl_30_ago = 0.0
    atl_30_ago = 0.0
    for i in range(89, 29, -1):
        date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        load = daily_load.get(date, 0)
        ctl_30_ago = ctl_30_ago + ctl_k * (load - ctl_30_ago)
        atl_30_ago = atl_30_ago + atl_k * (load - atl_30_ago)

    trend = ctl - ctl_30_ago
    if trend > 3:
        trend_text = "растущая форма"
    elif trend < -3:
        trend_text = "снижение формы"
    else:
        trend_text = "стабильная форма"

    # Интерпретация TSB
    if tsb > 10:
        form_text = "свежий (хорошая готовность)"
    elif tsb > -10:
        form_text = "нейтральный"
    elif tsb > -30:
        form_text = "накопленная усталость"
    else:
        form_text = "высокая усталость (риск перетренированности)"

    return {
        "ctl": round(ctl, 1),        # Fitness — чем выше тем лучше форма
        "atl": round(atl, 1),        # Fatigue — острая усталость
        "tsb": round(tsb, 1),        # Form — готовность (-30..+25)
        "trend": round(trend, 1),
        "trend_text": trend_text,
        "form_text": form_text,
        "summary": (
            f"Тренированность (CTL): {round(ctl, 1)}, "
            f"Усталость (ATL): {round(atl, 1)}, "
            f"Форма (TSB): {round(tsb, 1)} — {form_text}. "
            f"Тренд за месяц: {trend_text}."
        )
    }


async def get_best_efforts_and_predictions(access_token: str) -> dict:
    """
    Собирает best_efforts за последние 3 месяца и считает прогнозы
    на стандартные дистанции по формуле Риегеля.
    """
    activities = await get_recent_activities(access_token, days=90)
    runs = [a for a in activities if a.get("type") == "Run"]

    # Лучшие результаты по дистанциям за 3 месяца
    best = {}  # {"5km": {"time_sec": 1182, "pace": "3:56", "date": "2026-03-15"}}

    for activity in runs[:30]:  # ограничиваем 30 активностями
        detail = await get_activity_detail(access_token, activity["id"])
        if not detail:
            continue

        for effort in detail.get("best_efforts", []):
            name = effort.get("name")
            if name not in RACE_DISTANCES:
                continue

            elapsed = effort.get("elapsed_time", 0)
            distance = effort.get("distance", 0)
            date = effort.get("start_date", "")[:10]

            if name not in best or elapsed < best[name]["time_sec"]:
                best[name] = {
                    "time_sec": elapsed,
                    "time": _format_time(elapsed),
                    "pace": _calc_pace(distance, elapsed),
                    "date": date,
                }

    if not best:
        return {"best_efforts": {}, "predictions": {}, "base_distance": None}

    # Считаем прогнозы по формуле Риегеля: T2 = T1 * (D2/D1)^1.06
    predictions = _calculate_riegels_predictions(best)

    # Базовая дистанция для прогноза (лучшая из доступных)
    priority = ["10km", "5km", "Half-Marathon", "1 mile", "1km"]
    base_distance = next((d for d in priority if d in best), None)

    return {
        "best_efforts": best,
        "predictions": predictions,
        "base_distance": base_distance,
    }


def _calculate_riegels_predictions(best: dict) -> dict:
    """
    Формула Риегеля: T2 = T1 * (D2/D1)^1.06
    Рассчитывает прогнозы для всех стандартных дистанций.
    """
    # Находим лучшую базу для расчёта
    priority = ["10km", "5km", "Half-Marathon", "1 mile", "1km", "Marathon"]
    base = None
    for dist in priority:
        if dist in best:
            base = dist
            break

    if not base:
        return {}

    t1 = best[base]["time_sec"]
    d1 = RACE_DISTANCES[base]
    predictions = {}

    for name, d2 in RACE_DISTANCES.items():
        if name == base:
            predictions[name] = {
                "time": best[base]["time"],
                "pace": best[base]["pace"],
                "source": "actual"
            }
            continue

        t2 = t1 * (d2 / d1) ** 1.06
        predictions[name] = {
            "time": _format_time(int(t2)),
            "pace": _calc_pace(d2, t2),
            "source": "predicted"
        }

    return predictions


async def get_full_athlete_data(access_token: str) -> dict:
    """
    Собирает все данные атлета для передачи в AI:
    - Последние пробежки (14 дней)
    - CTL/ATL/TSB с трендом
    - Best efforts + прогнозы по Риегелю
    """
    import asyncio

    # Запускаем параллельно
    runs_task = get_recent_runs(access_token, days=14)
    load_task = get_training_load(access_token)

    runs, load = await asyncio.gather(runs_task, load_task)
    fitness = analyze_fitness(runs)

    # Best efforts — отдельно (много запросов, не параллелим)
    efforts = await get_best_efforts_and_predictions(access_token)

    return {
        "recent_runs": runs,
        "fitness": fitness,
        "training_load": load,
        "best_efforts": efforts.get("best_efforts", {}),
        "predictions": efforts.get("predictions", {}),
        "last_race": None,  # Strava workout_type ненадёжен; гонки определяются только по Garmin
    }


def analyze_fitness(runs: list) -> dict:
    """Анализ нагрузки за последние 2 недели"""
    if not runs:
        return {
            "total_km": 0, "run_count": 0, "avg_pace": "—",
            "avg_hr": None, "fatigue_level": "low",
            "summary": "Нет данных о пробежках за последние 2 недели."
        }

    total_km = sum(r["distance_km"] for r in runs)
    run_count = len(runs)

    hrs = [r["avg_hr"] for r in runs if r["avg_hr"]]
    avg_hr = round(sum(hrs) / len(hrs)) if hrs else None

    if total_km > 60 or run_count >= 6:
        fatigue_level = "high"
        fatigue_text = "высокая нагрузка за последние 2 недели"
    elif total_km > 30 or run_count >= 4:
        fatigue_level = "medium"
        fatigue_text = "умеренная нагрузка"
    else:
        fatigue_level = "low"
        fatigue_text = "лёгкая нагрузка"

    last_run = runs[0] if runs else None
    last_run_text = ""
    if last_run:
        last_run_text = (
            f"Последняя пробежка: {last_run['date']}, "
            f"{last_run['distance_km']} км, темп {last_run['avg_pace']} мин/км."
        )

    return {
        "total_km": round(total_km, 1),
        "run_count": run_count,
        "avg_pace": runs[0]["avg_pace"] if runs else "—",
        "avg_hr": avg_hr,
        "fatigue_level": fatigue_level,
        "summary": (
            f"За последние 2 недели: {run_count} пробежек, {round(total_km, 1)} км, {fatigue_text}. "
            f"{last_run_text}"
        )
    }


def _calc_pace(distance_m: float, time_sec: float) -> str:
    if not distance_m or not time_sec:
        return "—"
    pace_sec = time_sec / (distance_m / 1000)
    minutes = int(pace_sec // 60)
    seconds = int(pace_sec % 60)
    return f"{minutes}:{seconds:02d}"


def _format_time(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


# ── Слой 1.1: загрузка сырых данных ──────────────────────────

async def fetch_raw(db_user_id: int) -> dict | None:
    """Слой 1.1: тянет сырые данные Strava и сохраняет в raw_service_data as is.

    Strava — агрегатор: нагрузка 48ч, CTL/ATL/TSB, прогнозы забегов.
    НЕ источник биометрии. Парсинг — слой 2 (data_normalizer).
    """
    import json
    import asyncio
    import database as db

    token = await ensure_valid_token(db_user_id)
    if not token:
        print(f"Strava fetch_raw: нет токена для user_id={db_user_id}")
        return None

    athlete, load_48h = await asyncio.gather(
        get_full_athlete_data(token),
        get_recent_48h_load(token),
        return_exceptions=True,
    )

    def _clean(x):
        return None if isinstance(x, Exception) else x

    athlete = _clean(athlete) or {}
    raw = {
        "training_load": athlete.get("training_load"),
        "predictions":   athlete.get("predictions"),
        "best_efforts":  athlete.get("best_efforts"),
        "fitness":       athlete.get("fitness"),
        "load_48h":      _clean(load_48h),
    }

    if not any(v is not None for v in raw.values()):
        print(f"Strava fetch_raw: нет данных для user_id={db_user_id}")
        return None

    db.save_raw_service_data(db_user_id, "strava", json.dumps(raw, ensure_ascii=False, default=str))
    got = [k for k, v in raw.items() if v is not None]
    print(f"Strava fetch_raw: сохранено для user_id={db_user_id} ({', '.join(got)})")
    return raw


# ── Профиль: пол (Strava даёт пол, дату рождения — нет) ──────

async def get_profile(db_user_id: int) -> dict | None:
    """Пол из Strava /athlete в едином формате.

    Возвращает {gender: 'male'/'female'} или None.
    Strava sex = M/F. Дату рождения Strava не отдаёт.
    """
    import aiohttp as _aiohttp

    token = await ensure_valid_token(db_user_id)
    if not token:
        return None

    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with _aiohttp.ClientSession() as s:
            async with s.get("https://www.strava.com/api/v3/athlete", headers=headers) as r:
                if r.status != 200:
                    return None
                d = await r.json()
    except Exception as e:
        print(f"Strava get_profile error: {e}")
        return None

    sex = d.get("sex")
    if not sex:
        return None
    return {"gender": "female" if str(sex).upper() == "F" else "male"}


if __name__ == "__main__":
    print("Strava auth URL:")
    print(get_auth_url(123456789))