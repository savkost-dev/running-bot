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


# Коэффициент перевода VO2max с часов в VDOT.
# VO2max — оценка потенциала прибором, VDOT — индекс по результату,
# разница между ними — экономичность бега. Приравнивание (k=1.0) завышало зоны
# у тех, у кого нет ЛП.
# ⚠ ВРЕМЕННАЯ КОНСТАНТА (05.08.2026): плоские 0.95 до набора статистики по клубу.
# План: k(VO2max) по наблюдённым парам VO2max↔ЛП. См. PROCESS_MAP.md.
K_VO2MAX = 0.95


def resolve_anchor(profile: dict | None) -> dict | None:
    """Точка отсчёта для зон по лесенке приоритетов (решение 05.08.2026):
      1. ЛП введён вручную → якорь ЛП (прямое указание темпа, без преобразований)
      2. VO2max введён вручную → VO2max × k
      3. VO2max с часов → VO2max × k  (базовый сценарий: часы обновляют его часто)
      4. ЛП с часов → якорь ЛП (фолбэк, когда VO2max нет вовсе)
    Возвращает {"kind", "vdot", "text"} или None.
    """
    p = profile or {}
    lt = p.get("lactate_threshold_pace")
    lt_manual = (p.get("lactate_source") or "").lower() == "manual"
    vo2 = p.get("vo2max")
    vo2_manual = (p.get("vo2max_source") or "").lower() == "manual"

    if lt and lt_manual:
        vdot = _vdot_from_threshold_pace(lt)
        if vdot:
            return {"kind": "lt_manual", "vdot": vdot,
                    "text": f"лактатный порог {lt} (вручную)"}
    if vo2:
        try:
            vdot = float(vo2) * K_VO2MAX
        except (TypeError, ValueError):
            vdot = None
        if vdot and vdot > 0:
            src = "вручную" if vo2_manual else (p.get("vo2max_source") or "часы")
            return {"kind": "vo2max_manual" if vo2_manual else "vo2max_device", "vdot": vdot,
                    "text": f"VO2max {vo2} ({src}) × {K_VO2MAX}"}
    if lt:
        vdot = _vdot_from_threshold_pace(lt)
        if vdot:
            return {"kind": "lt_device", "vdot": vdot,
                    "text": f"лактатный порог {lt} ({p.get('lactate_source') or 'часы'})"}
    return None


def calculate_pace_zones(user_profile: dict | None,
                         athlete_cache: dict | None) -> dict | None:
    """Считает персональные зоны. Приоритет — в resolve_anchor, дальше Риегель.

    Возвращает {"zones": {...}, "source": kind, "vdot": float, "anchor": text} или None.
    """
    anchor = resolve_anchor(user_profile)
    if anchor:
        return {"zones": _zones_from_vdot(anchor["vdot"]), "source": anchor["kind"],
                "vdot": anchor["vdot"], "anchor": anchor["text"]}

    # Риегель — прогнозы забегов (когда нет ни VO2max, ни ЛП)
    predictions = (athlete_cache or {}).get("predictions") or {}
    vdot = _vdot_from_predictions(predictions)
    if vdot:
        return {"zones": _zones_from_vdot(vdot), "source": "riegel",
                "vdot": vdot, "anchor": "прогнозы забегов Strava"}
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


# Темп повторов по длине отрезка — доля от зоны repetition.
# Это НЕ спринтерский максимум (его из аэробных чисел не вывести), а максимально
# ПОВТОРЯЕМЫЙ темп для отрезка данной длины (серия с полным отдыхом):
# у Дэниелса R-темп задан для 400-800м, на 200 и 100м тот же бегун повторяет быстрее,
# но не пропорционально — поэтому свой коэффициент на каждую длину.
# Источник/проверка/опровержение — в PROCESS_MAP.md, раздел о повторяемом темпе.
_REPEAT_FACTORS = {400: 1.00, 300: 0.96, 200: 0.93, 100: 0.88}
_TYPE_SHIFT = {"скоростной": -0.02, "универсал": 0.0, "выносливостный": 0.02}


def runner_type(zones: dict) -> str | None:
    """Тип бегуна по разрыву threshold−repetition: >30 сек — скоростной,
    15-30 — универсал, <15 — выносливостный. Сдвигает короткий край таблицы."""
    rep = _pace_to_sec_per_km((zones or {}).get("repetition"))
    thr = _pace_to_sec_per_km((zones or {}).get("threshold"))
    if not rep or not thr:
        return "универсал" if rep else None
    gap = thr - rep
    return "скоростной" if gap > 30 else ("универсал" if gap >= 15 else "выносливостный")


def repeat_pace_for_distance(repetition_pace: str, distance_m: int,
                             rtype: str | None = None) -> str | None:
    """Максимально повторяемый темп для отрезка 100-400м (серия повторов).
    Коэффициент берётся из таблицы с линейной интерполяцией между узлами,
    тип бегуна сдвигает короткий край (на 400м сдвига нет, на 100м максимальный).
    """
    rep_sec = _pace_to_sec_per_km(repetition_pace)
    if not rep_sec:
        return None
    d = max(100, min(400, int(distance_m or 0)))
    nodes = sorted(_REPEAT_FACTORS)
    lo = max([n for n in nodes if n <= d])
    hi = min([n for n in nodes if n >= d])
    if lo == hi:
        factor = _REPEAT_FACTORS[lo]
    else:
        w = (d - lo) / (hi - lo)
        factor = _REPEAT_FACTORS[lo] + w * (_REPEAT_FACTORS[hi] - _REPEAT_FACTORS[lo])
    factor += _TYPE_SHIFT.get(rtype or "универсал", 0.0) * (400 - d) / 300.0
    return _sec_per_km_to_pace(rep_sec * factor)


def get_pace_zones(db_user_id: int) -> dict | None:
    """Достаёт готовые зоны из athlete_cache.
    Если зон нет (новый пользователь) — считает на лету и сохраняет.
    Возвращает {"zones": dict, "source": str, "updated_at": str} или None.

    ПРИМЕЧАНИЕ (05.08.2026): скоростной паспорт (k100) и потолок на коротких
    отрезках УДАЛЕНЫ. Максимальную скорость на 100-400м нельзя вывести из аэробных
    показателей: модель критической скорости неприменима ниже 800м, таблиц Дэниелса
    для этой величины не существует, а спринтерская скорость измеряется, а не считается.
    Прежняя формула (DeepSeek, июнь 2026) давала на 400м темп МЕДЛЕННЕЕ повторного
    и противоречила фактам. Вернёмся к вопросу только через измерение
    (ручной ввод лучшего результата либо фактические лэпы из разборов).
    """
    import database as _db
    cached = _db.get_pace_zones_raw(db_user_id)
    if not cached:
        result = recalculate_and_save(db_user_id)
        if result:
            cached = _db.get_pace_zones_raw(db_user_id)
    if cached:
        cached["runner_type"] = runner_type(cached.get("zones") or {})
    return cached
