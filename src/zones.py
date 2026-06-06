"""Персональные темповые зоны бегуна (подход Джека Дэниелса / VDOT).

Зоны — свойство человека, не зависят от конкретной тренировки.
Считаются один раз при обновлении исходных данных и хранятся в athlete_cache.

Источник (по приоритету):
  1. lactate  — лактатный (пороговый) темп из профиля
  2. vo2max   — VO2max из профиля/трекера → VDOT
  3. riegel   — прогнозные времена забегов (predictions из athlete_cache)
"""
import math
import logging

logger = logging.getLogger(__name__)

# Доля VDOT (≈ %VO2max) для каждой зоны по Дэниелсу
_ZONE_FRACTIONS = {
    "easy":       0.70,   # E — лёгкий / восстановительный
    "marathon":   0.84,   # M — марафонский
    "threshold":  0.88,   # T — пороговый (темповый)
    "interval":   0.98,   # I — интервальный (МПК)
    "repetition": 1.06,   # R — повторный (скорость)
}

# Коэффициенты Дэниелса: VO2(ml/kg/min) от скорости v (м/мин)
_A, _B, _C = 0.000104, 0.182258, -4.60


def _pace_to_sec_per_km(pace: str) -> float | None:
    """'4:17' или '4.17' → 257.0 секунд на км."""
    if not pace:
        return None
    s = str(pace).strip().replace(".", ":")
    if ":" not in s:
        return None
    try:
        mm, ss = s.split(":")[:2]
        return int(mm) * 60 + int(ss)
    except (ValueError, IndexError):
        return None


def _sec_per_km_to_pace(sec: float) -> str:
    """257.0 → '4:17'."""
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _vo2_at_velocity(v: float) -> float:
    """VO2 (ml/kg/min) при скорости v (м/мин)."""
    return _A * v * v + _B * v + _C


def _velocity_at_vo2(target_vo2: float) -> float:
    """Скорость (м/мин), дающая заданное VO2 — корень квадратного уравнения."""
    c = _C - target_vo2
    disc = _B * _B - 4 * _A * c
    if disc < 0:
        disc = 0
    return (-_B + math.sqrt(disc)) / (2 * _A)


def _velocity_from_sec_per_km(sec: float) -> float:
    """Темп (сек/км) → скорость (м/мин)."""
    return 60000.0 / sec


def _zones_from_vdot(vdot: float) -> dict:
    """Строит все зоны (темпы мин/км) из VDOT."""
    zones = {}
    for name, frac in _ZONE_FRACTIONS.items():
        v = _velocity_at_vo2(frac * vdot)
        if v <= 0:
            continue
        zones[name] = _sec_per_km_to_pace(60000.0 / v)
    return zones


def _vdot_from_threshold_pace(pace: str) -> float | None:
    """VDOT из порогового темпа (T ≈ 0.88·VDOT)."""
    sec = _pace_to_sec_per_km(pace)
    if not sec or sec <= 0:
        return None
    v = _velocity_from_sec_per_km(sec)
    vo2_t = _vo2_at_velocity(v)
    return vo2_t / _ZONE_FRACTIONS["threshold"]


def _parse_race_time(t: str) -> float | None:
    """'MM:SS' или 'H:MM:SS' → секунды."""
    if not t:
        return None
    parts = str(t).strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return None


def _vdot_from_predictions(predictions: dict) -> float | None:
    """VDOT из прогнозного времени забега (формула Дэниелса от Риегеля)."""
    if not predictions:
        return None
    dist_map = {"5km": 5000, "10km": 10000, "1 mile": 1609, "21km": 21097, "half": 21097}
    for name, dist_m in dist_map.items():
        p = predictions.get(name)
        if not p or not isinstance(p, dict):
            continue
        t_sec = _parse_race_time(p.get("time"))
        if not t_sec or t_sec <= 0:
            continue
        t_min = t_sec / 60.0
        v = dist_m / t_min  # м/мин
        vo2 = _vo2_at_velocity(v)
        pct = (0.8 + 0.1894393 * math.exp(-0.012778 * t_min)
               + 0.2989558 * math.exp(-0.1932605 * t_min))
        if pct <= 0:
            continue
        return vo2 / pct
    return None


def calculate_pace_zones(user_profile: dict | None,
                         athlete_cache: dict | None) -> dict | None:
    """Считает персональные зоны. Приоритет: лактат → VO2max → Риегель.

    Возвращает {"zones": {...}, "source": "lactate"|"vo2max"|"riegel"} или None.
    """
    profile = user_profile or {}

    # 1. Лактатный (пороговый) темп
    lt_pace = profile.get("lactate_threshold_pace")
    if lt_pace:
        vdot = _vdot_from_threshold_pace(lt_pace)
        if vdot:
            return {"zones": _zones_from_vdot(vdot), "source": "lactate"}

    # 2. VO2max → VDOT напрямую
    vo2max = profile.get("vo2max")
    if vo2max:
        try:
            vdot = float(vo2max)
            if vdot > 0:
                return {"zones": _zones_from_vdot(vdot), "source": "vo2max"}
        except (TypeError, ValueError):
            pass

    # 3. Риегель — прогнозы забегов
    predictions = (athlete_cache or {}).get("predictions") or {}
    vdot = _vdot_from_predictions(predictions)
    if vdot:
        return {"zones": _zones_from_vdot(vdot), "source": "riegel"}

    return None


def recalculate_and_save(db_user_id: int) -> dict | None:
    """Пересчитывает зоны из текущих данных и сохраняет в athlete_cache.
    Вызывать при обновлении VO2max / лактатного порога. Возвращает результат или None.
    """
    import database as _db
    profile = _db.get_user_profile(db_user_id)
    cache = _db.get_athlete_cache(db_user_id)
    result = calculate_pace_zones(profile, cache)
    if result:
        _db.save_pace_zones(db_user_id, result["zones"], result["source"])
        logger.info(
            f"Зоны пересчитаны для user {db_user_id}: source={result['source']}, "
            f"threshold={result['zones'].get('threshold')}"
        )
    return result


def _compute_speed_passport(zones: dict) -> tuple[float | None, str | None]:
    """Скоростной паспорт бегуна из зон.
    Возвращает (k100, runner_profile).
    k100 — коэффициент скорости на 100м относительно repetition-зоны:
      gap threshold−repetition > 30 сек → 0.55 (скоростной)
      gap 15−30 сек             → 0.64 (универсал)
      gap < 15 сек              → 0.73 (выносливостный)
    """
    rep = _pace_to_sec_per_km(zones.get("repetition"))
    thr = _pace_to_sec_per_km(zones.get("threshold"))
    if not rep:
        return None, None
    if thr:
        gap = thr - rep   # сек/км, > 0 (threshold медленнее repetition)
        if gap > 30:
            return 0.55, "скоростной"
        elif gap >= 15:
            return 0.64, "универсал"
        else:
            return 0.73, "выносливостный"
    return 0.64, "универсал"


def speed_ceiling_for_distance(repetition_pace: str, k100: float, distance_m: int) -> str | None:
    """Потолок финишного темпа для скоростного отрезка заданной дистанции (100–400м).
    Формула критической скорости:
      k(d) = 1 - (1 - k100) × (400 - d) / (3 × d)
      MSS(d) = repetition_sec × k(d)
      ceiling = MSS(d) / 0.94   (целевой темп ≤ 94% MSS — держимый без срыва техники)
    """
    rep_sec = _pace_to_sec_per_km(repetition_pace)
    if not rep_sec or not k100:
        return None
    d = max(100, min(400, distance_m))
    k = 1 - (1 - k100) * (400 - d) / (3 * d)
    ceiling_sec = int(rep_sec * k / 0.94)
    return _sec_per_km_to_pace(ceiling_sec)


def get_pace_zones(db_user_id: int) -> dict | None:
    """Достаёт готовые зоны из athlete_cache.
    Если зон нет (новый пользователь) — считает на лету и сохраняет.
    Возвращает {"zones": dict, "source": str, "updated_at": str,
                "speed_k100": float|None, "runner_profile": str|None} или None.
    """
    import database as _db
    cached = _db.get_pace_zones_raw(db_user_id)
    if not cached:
        result = recalculate_and_save(db_user_id)
        if result:
            cached = _db.get_pace_zones_raw(db_user_id)
    if cached:
        k100, profile = _compute_speed_passport(cached.get("zones") or {})
        cached["speed_k100"] = k100
        cached["runner_profile"] = profile
    return cached
