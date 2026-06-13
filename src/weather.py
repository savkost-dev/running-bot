import os
import re
import aiohttp
from datetime import datetime, date

OPENWEATHER_KEY = os.getenv("OPENWEATHER_API_KEY", "")

_geo_cache: dict[str, tuple[float, float] | None] = {}


async def _geocode(location: str) -> tuple[float, float] | None:
    if location in _geo_cache:
        return _geo_cache[location]
    if not OPENWEATHER_KEY:
        _geo_cache[location] = None
        return None

    # Вытаскиваем название метро из скобок до их удаления
    metro_m = re.search(r'\(м\.?\s*([^)]+)\)', location, re.IGNORECASE)
    metro_name = metro_m.group(1).strip() if metro_m else None

    clean = re.sub(r'\(.*?\)', '', location).strip().rstrip(',').strip()
    parts = [p.strip() for p in clean.split(',') if p.strip()]

    # Кандидаты: пробуем отбрасывать ведущие части (название здания, улица с номером)
    candidates = []
    for i in range(len(parts)):
        q = ', '.join(parts[i:])
        if not any(c in q.lower() for c in ('москва', 'moscow', 'санкт', 'питер', 'спб')):
            q += ', Москва'
        if q not in candidates:
            candidates.append(q)

    # Пробуем улицу без номера дома (убираем "24с1", "12А" и т.п.)
    if parts:
        no_num = re.sub(r'\s+\d+\S*$', '', parts[0]).strip()
        if no_num and no_num != parts[0]:
            q = no_num + ', Москва'
            if q not in candidates:
                candidates.insert(1, q)

    # Название метро как запасной вариант
    if metro_name:
        q = metro_name + ', Москва'
        if q not in candidates:
            candidates.append(q)

    # Также пробуем просто "Москва" если есть хоть какой-то московский маркер
    has_moscow_hint = any(c in location.lower() for c in (
        'москв', 'moscow', 'м.', 'метро', 'проспект', 'улица', 'бульвар',
        'набережная', 'переулок', 'шоссе', 'площадь', 'парк', 'стадион',
    ))
    if has_moscow_hint and 'Москва' not in candidates:
        candidates.append('Москва')

    for query in candidates:
        url = (
            "http://api.openweathermap.org/geo/1.0/direct"
            f"?q={query}&limit=1&appid={OPENWEATHER_KEY}"
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    if data:
                        result = (float(data[0]["lat"]), float(data[0]["lon"]))
                        _geo_cache[location] = result
                        return result
        except Exception:
            pass

    _geo_cache[location] = None
    return None


def _extract_start_time(schedule: str) -> str:
    if not schedule:
        return "08:00"
    m = re.search(r'(\d{1,2}:\d{2})\s*[-–]\s*старт', schedule, re.IGNORECASE)
    if m:
        return m.group(1).zfill(5)
    m = re.search(r'(\d{1,2}:\d{2})', schedule)
    if m:
        return m.group(1).zfill(5)
    return "08:00"


async def get_weather_for_workout(
    location: str, workout_date: str, schedule: str = ""
) -> dict | None:
    if not OPENWEATHER_KEY or not location:
        return None

    coords = await _geocode(location)
    if not coords:
        return None

    lat, lon = coords
    workout_time = _extract_start_time(schedule)

    try:
        workout_dt = datetime.strptime(f"{workout_date} {workout_time}", "%Y-%m-%d %H:%M")
    except Exception:
        return None

    days_ahead = (workout_dt.date() - date.today()).days

    if days_ahead < 0:
        timing = "past"
    elif days_ahead == 0:
        timing = "today"
    else:
        timing = "forecast"

    if days_ahead <= 0:
        url = (
            f"https://api.openweathermap.org/data/2.5/weather"
            f"?lat={lat}&lon={lon}&appid={OPENWEATHER_KEY}&units=metric&lang=ru"
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    result = _parse_current(await resp.json())
                    if result:
                        result["timing"] = timing
                    return result
        except Exception:
            return None
    elif days_ahead <= 5:
        url = (
            f"https://api.openweathermap.org/data/2.5/forecast"
            f"?lat={lat}&lon={lon}&appid={OPENWEATHER_KEY}&units=metric&lang=ru"
        )
        try:
            from datetime import timedelta
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    result = _parse_forecast(data, workout_dt)
                    if result:
                        result["timing"] = timing
                        # Прогноз через 2 ч после старта (летом утром быстро теплеет).
                        # Берём из того же ответа forecast — без второго запроса.
                        plus2 = _parse_forecast(data, workout_dt + timedelta(hours=2))
                        if plus2:
                            result["temp_plus2h"] = plus2["temp"]
                            result["feels_like_plus2h"] = plus2["feels_like"]
                    return result
        except Exception:
            return None
    return None


def _parse_current(data: dict) -> dict | None:
    try:
        main = data.get("main", {})
        wind = data.get("wind", {})
        w = data.get("weather", [{}])[0]
        rain = data.get("rain", {})
        snow = data.get("snow", {})
        precip = rain.get("1h", 0) + rain.get("3h", 0) + snow.get("1h", 0) + snow.get("3h", 0)
        return {
            "temp": round(main.get("temp", 0)),
            "feels_like": round(main.get("feels_like", 0)),
            "humidity": main.get("humidity", 0),
            "wind_speed": round(wind.get("speed", 0), 1),
            "precipitation_mm": round(precip, 1),
            "description": w.get("description", ""),
            "icon": w.get("icon", ""),
        }
    except Exception:
        return None


def _parse_forecast(data: dict, target_dt: datetime) -> dict | None:
    try:
        forecasts = data.get("list", [])
        if not forecasts:
            return None
        best = min(forecasts, key=lambda f: abs(datetime.fromtimestamp(f["dt"]) - target_dt))
        main = best.get("main", {})
        wind = best.get("wind", {})
        w = best.get("weather", [{}])[0]
        rain = best.get("rain", {})
        snow = best.get("snow", {})
        precip = rain.get("3h", 0) + snow.get("3h", 0)
        return {
            "temp": round(main.get("temp", 0)),
            "feels_like": round(main.get("feels_like", 0)),
            "humidity": main.get("humidity", 0),
            "wind_speed": round(wind.get("speed", 0), 1),
            "precipitation_mm": round(precip, 1),
            "description": w.get("description", ""),
            "icon": w.get("icon", ""),
        }
    except Exception:
        return None


def format_weather_for_prompt(weather: dict) -> str:
    if not weather:
        return ""
    temp = weather.get("temp", "?")
    feels = weather.get("feels_like", "?")
    wind = weather.get("wind_speed", 0)
    precip = weather.get("precipitation_mm", 0)
    humidity = weather.get("humidity", 0)
    desc = weather.get("description", "")
    precip_str = f"{precip} мм" if precip > 0 else "нет"
    lines = [
        f"Температура: {temp}°C (ощущается как {feels}°C)",
        f"Осадки: {precip_str}",
        f"Ветер: {wind} м/с",
        f"Влажность: {humidity}%",
    ]
    if desc:
        lines.append(f"Условия: {desc}")
    return "\n".join(lines)


def format_weather_for_message(weather: dict) -> str:
    if not weather:
        return ""
    temp = weather.get("temp", "?")
    wind = weather.get("wind_speed", 0)
    precip = weather.get("precipitation_mm", 0)
    icon = weather.get("icon", "")

    if "01" in icon:
        emoji = "☀️"
    elif "02" in icon or "03" in icon:
        emoji = "⛅"
    elif "04" in icon:
        emoji = "☁️"
    elif "09" in icon or "10" in icon:
        emoji = "🌧"
    elif "11" in icon:
        emoji = "⛈"
    elif "13" in icon:
        emoji = "❄️"
    elif "50" in icon:
        emoji = "🌫"
    else:
        emoji = "🌤"

    parts = [f"{emoji} {temp}°C"]
    if wind >= 1:
        parts.append(f"ветер {wind} м/с")
    parts.append("без осадков" if precip == 0 else f"осадки {precip} мм")

    if weather.get("timing") == "past":
        parts.append("(текущая погода)")

    return ", ".join(parts)
