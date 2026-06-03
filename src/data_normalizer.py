"""
data_normalizer.py — Слой 2: нормализация данных сервисов в единый формат.

Изолирован от прода — не импортируется нигде до явного переключения.
Каждый normalizer принимает сырой dict (raw_json из БД) и возвращает UnifiedUserData.

Префиксы полей: s3_ — готовы к потреблению промптом (слой 3).
Соответствует таблице мэппинга в DATA_NORMALIZER_SPEC.md.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json
import math
import logging

logger = logging.getLogger(__name__)


# ── Вложенные структуры ───────────────────────────────────────

@dataclass
class LoadRecent:
    """Недавняя нагрузка — последние тренировки (окно 48ч)."""
    sessions:       int   = 0
    total_km:       float = 0.0
    last_hours_ago: int | None = None
    intensity:      str = "unknown"   # low / moderate / high / unknown


@dataclass
class LoadChronic:
    """Хроническая нагрузка — CTL/ATL/TSB (модель Banister)."""
    ctl:     float | None = None   # хроническая (42 дня)
    atl:     float | None = None   # острая (7 дней)
    tsb:     float | None = None   # форма = ctl - atl
    summary: str = ""


# ── Выходной формат (слой 3) ──────────────────────────────────

@dataclass
class UnifiedUserData:
    # Тренированность
    s3_vo2max:                 float | None = None  # мл/кг/мин
    s3_lactate_threshold_pace: str | None   = None  # "4:04" мин:сек/км
    s3_lactate_threshold_hr:   int | None   = None  # уд/мин
    s3_zones:                  dict | None  = None  # E/M/T/I/R

    # Восстановление
    s3_recovery_daily: int | None   = None  # 0–100, суточное
    s3_recovery_total: float | None = None  # длительное (TSB или 0–100 от TR)
    s3_hrv:            float | None = None  # мс, ночной
    s3_hrv_baseline:   float | None = None  # 7-дневная база
    s3_rhr:            int | None   = None  # ЧСС покоя

    # Нагрузка
    s3_load_recent:  LoadRecent | None  = None
    s3_load_chronic: LoadChronic | None = None

    # Мета
    sources:    list = field(default_factory=list)
    updated_at: str  = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, s: str) -> UnifiedUserData:
        d = json.loads(s)
        if d.get("s3_load_recent"):
            d["s3_load_recent"] = LoadRecent(**d["s3_load_recent"])
        if d.get("s3_load_chronic"):
            d["s3_load_chronic"] = LoadChronic(**d["s3_load_chronic"])
        return cls(**d)


# ── Вспомогательные функции ───────────────────────────────────

def _sec_to_pace(sec: int) -> str | None:
    """244 → '4:04'"""
    if not sec or sec <= 0:
        return None
    return f"{sec // 60}:{sec % 60:02d}"


def _pace_to_sec(pace: str) -> int | None:
    """'4:04' → 244"""
    if not pace:
        return None
    try:
        parts = str(pace).strip().replace(".", ":").split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return None


def _vdot_from_ltsp(ltsp_sec: int) -> float | None:
    """VDOT из лактатного порога (сек/км). T-зона ≈ 0.88 × VDOT по Дэниелсу."""
    if not ltsp_sec or ltsp_sec <= 0:
        return None
    v = 60000.0 / ltsp_sec  # м/мин
    vo2_t = 0.000104 * v**2 + 0.182258 * v - 4.60
    return round(vo2_t / 0.88, 1) if vo2_t > 0 else None


def _vdot_from_race(dist_m: int, time_sec: int) -> float | None:
    """VDOT из результата на дистанции (формула Дэниелса)."""
    if dist_m <= 0 or time_sec <= 0:
        return None
    t_min = time_sec / 60.0
    v = dist_m / t_min
    vo2 = 0.000104 * v**2 + 0.182258 * v - 4.60
    pct = (0.8 + 0.1894393 * math.exp(-0.012778 * t_min)
           + 0.2989558 * math.exp(-0.1932605 * t_min))
    return round(vo2 / pct, 1) if pct > 0 else None


def _zones_from_vdot(vdot: float) -> dict:
    """Зоны E/M/T/I/R из VDOT по Дэниелсу."""
    fracs = {"easy": 0.70, "marathon": 0.84, "threshold": 0.88,
             "interval": 0.98, "repetition": 1.06}
    A, B, C = 0.000104, 0.182258, -4.60
    zones = {}
    for name, frac in fracs.items():
        target = frac * vdot
        disc = B**2 - 4 * A * (C - target)
        if disc < 0:
            continue
        v = (-B + math.sqrt(disc)) / (2 * A)
        if v > 0:
            sec = int(round(60000.0 / v))
            zones[name] = f"{sec // 60}:{sec % 60:02d}"
    return zones


def _ans_to_recovery(ans_charge: float) -> int:
    """Polar ANS charge (-10..+10) → recovery 0–100."""
    return max(0, min(100, round((ans_charge + 10) / 20 * 100)))


def _intensity_from_load(load_val: float | None) -> str:
    if load_val is None:
        return "unknown"
    if load_val < 50:
        return "low"
    if load_val <= 150:
        return "moderate"
    return "high"


def _intensity_from_pace_vs_lt(avg_pace: str | None, lt_pace: str | None) -> str:
    p = _pace_to_sec(avg_pace)
    lt = _pace_to_sec(lt_pace)
    if not p or not lt:
        return "unknown"
    if p < lt * 0.97:
        return "high"
    if p < lt * 1.05:
        return "moderate"
    return "low"


def _tsb_summary(tsb: float | None) -> str:
    if tsb is None:
        return ""
    return ("свежий" if tsb > 5
            else "перегрузка" if tsb < -20
            else "небольшая усталость")


# ── Нормализаторы ─────────────────────────────────────────────

def normalize_garmin(raw: dict) -> UnifiedUserData:
    """Garmin → UnifiedUserData.

    raw — объединённый dict: get_full_data + lactate_threshold + recovery_cache.
    """
    u = UnifiedUserData(sources=["garmin"])

    # ── Тренированность ──
    # vo2max: прямой
    if raw.get("vo2max"):
        u.s3_vo2max = round(float(raw["vo2max"]), 1)
    # ЛП: прямой
    u.s3_lactate_threshold_pace = raw.get("lactate_threshold_pace")
    u.s3_lactate_threshold_hr   = raw.get("lactate_threshold_hr")
    # zones: расчёт через zones.py (тут — из vo2max если нет готовых)
    zones_raw = raw.get("pace_zones_json")
    if zones_raw:
        try:
            u.s3_zones = json.loads(zones_raw) if isinstance(zones_raw, str) else zones_raw
        except Exception:
            pass
    if not u.s3_zones:
        sec = _pace_to_sec(u.s3_lactate_threshold_pace)
        if sec:
            vdot = _vdot_from_ltsp(sec)
            if vdot:
                u.s3_zones = _zones_from_vdot(vdot)
        elif u.s3_vo2max:
            u.s3_zones = _zones_from_vdot(u.s3_vo2max)

    # ── Восстановление ──
    # recovery_daily: body_battery (прямой, но в промпте отключён)
    bb = raw.get("body_battery")
    if bb is not None:
        u.s3_recovery_daily = int(bb)
    # recovery_total: Training Readiness score (прямой, 0–100)
    tr = raw.get("training_readiness") or {}
    if isinstance(tr, dict) and tr.get("score") is not None:
        u.s3_recovery_total = float(tr["score"])
    # hrv
    hrv = raw.get("hrv")
    if isinstance(hrv, dict):
        u.s3_hrv          = hrv.get("hrv_last_night")
        u.s3_hrv_baseline = hrv.get("hrv_weekly_avg")
    else:
        u.s3_hrv = hrv
        hrv_status = raw.get("hrv_status") or {}
        if isinstance(hrv_status, dict):
            u.s3_hrv_baseline = hrv_status.get("hrv_weekly_avg")
    # rhr
    if raw.get("rhr"):
        u.s3_rhr = int(raw["rhr"])

    # ── Нагрузка ──
    load = raw.get("load_48h")
    if load and load.get("sessions_48h", 0) > 0:
        intensity = (_intensity_from_load(load.get("suffer_48h"))
                     if load.get("suffer_48h", 0) > 0
                     else _intensity_from_pace_vs_lt(load.get("avg_pace"),
                                                     u.s3_lactate_threshold_pace))
        u.s3_load_recent = LoadRecent(
            sessions       = load.get("sessions_48h", 0),
            total_km       = load.get("total_km_48h", load.get("km_48h", 0)),
            last_hours_ago = load.get("last_activity_hours_ago"),
            intensity      = intensity,
        )
    # load_chronic: training_load (своя метрика Garmin)
    tl = raw.get("training_load") or {}
    if tl:
        ctl = tl.get("ctl"); atl = tl.get("atl"); tsb = tl.get("tsb")
        if tsb is None and ctl is not None and atl is not None:
            tsb = round(float(ctl) - float(atl), 1)
        if ctl is not None or atl is not None:
            u.s3_load_chronic = LoadChronic(
                ctl=ctl, atl=atl, tsb=tsb, summary=tl.get("summary", _tsb_summary(tsb))
            )

    logger.info(f"normalize_garmin: vo2max={u.s3_vo2max} lt={u.s3_lactate_threshold_pace} "
                f"rec_daily={u.s3_recovery_daily} rec_total={u.s3_recovery_total} hrv={u.s3_hrv}")
    return u


def normalize_coros(raw: dict) -> UnifiedUserData:
    """COROS → UnifiedUserData.

    raw — dict из get_dashboard_data() (+ активности для load_recent).
    """
    u = UnifiedUserData(sources=["coros"])

    # ── Тренированность ──
    ltsp_pace = raw.get("lactate_threshold_pace")  # уже "4:04"
    ltsp_sec  = raw.get("ltsp")
    u.s3_lactate_threshold_pace = ltsp_pace
    u.s3_lactate_threshold_hr   = raw.get("lactate_threshold_hr") or raw.get("lthr")

    # vo2max: нативный или расчёт из ltsp
    if raw.get("vo2max") and float(raw["vo2max"]) > 0:
        u.s3_vo2max = round(float(raw["vo2max"]), 1)
    else:
        sec = int(ltsp_sec) if ltsp_sec else _pace_to_sec(ltsp_pace)
        if sec:
            u.s3_vo2max = _vdot_from_ltsp(sec)

    # zones из ltsp
    sec = int(ltsp_sec) if ltsp_sec else _pace_to_sec(u.s3_lactate_threshold_pace)
    if sec:
        vdot = _vdot_from_ltsp(sec)
        if vdot:
            u.s3_zones = _zones_from_vdot(vdot)

    # ── Восстановление ──
    # recovery_daily: recoveryPct (суточный, 0–100)
    if raw.get("recovery_score") is not None:
        u.s3_recovery_daily = int(raw["recovery_score"])
    # recovery_total: ati/cti (когда API заработает) — пока нет
    # hrv
    if raw.get("hrv"):
        u.s3_hrv = float(raw["hrv"])
    if raw.get("hrv_baseline"):
        u.s3_hrv_baseline = float(raw["hrv_baseline"])
    # rhr
    if raw.get("rhr"):
        u.s3_rhr = int(raw["rhr"])

    # ── Нагрузка ──
    load = raw.get("load_48h")
    if load and load.get("sessions_48h", 0) > 0:
        tl_val = load.get("avg_training_load")
        intensity = (_intensity_from_load(tl_val) if tl_val
                     else _intensity_from_pace_vs_lt(load.get("avg_pace"),
                                                     u.s3_lactate_threshold_pace))
        u.s3_load_recent = LoadRecent(
            sessions       = load.get("sessions_48h", 0),
            total_km       = load.get("total_km_48h", 0),
            last_hours_ago = load.get("last_activity_hours_ago"),
            intensity      = intensity,
        )
    # load_chronic: ati/cti из EvoLab (когда придут)
    ati = raw.get("ati"); cti = raw.get("cti")
    if ati is not None or cti is not None:
        ctl = float(cti) if cti is not None else None
        atl = float(ati) if ati is not None else None
        tsb = round(ctl - atl, 1) if ctl is not None and atl is not None else None
        u.s3_load_chronic = LoadChronic(ctl=ctl, atl=atl, tsb=tsb, summary=_tsb_summary(tsb))

    logger.info(f"normalize_coros: vo2max={u.s3_vo2max} lt={u.s3_lactate_threshold_pace} "
                f"rec_daily={u.s3_recovery_daily} hrv={u.s3_hrv}")
    return u


def normalize_polar(raw: dict) -> UnifiedUserData:
    """Polar → UnifiedUserData. Дополнительный сервис (один пользователь).

    raw — dict из nightly-recharge + vo2max. Нагрузку не даёт — нужна Strava.
    """
    u = UnifiedUserData(sources=["polar"])

    # vo2max: прямой
    if raw.get("vo2max") and float(raw["vo2max"]) > 0:
        u.s3_vo2max = round(float(raw["vo2max"]), 1)
        u.s3_zones  = _zones_from_vdot(u.s3_vo2max)

    # ЛП — если Polar Flow его отдаёт (уточняется)
    if raw.get("lactate_threshold_pace"):
        u.s3_lactate_threshold_pace = raw["lactate_threshold_pace"]
    if raw.get("lactate_threshold_hr"):
        u.s3_lactate_threshold_hr = raw["lactate_threshold_hr"]

    # recovery_daily: ANS charge → 0–100
    ans = raw.get("ans_charge")
    if ans is not None:
        u.s3_recovery_daily = _ans_to_recovery(float(ans))
    elif raw.get("recovery_score") is not None:
        u.s3_recovery_daily = int(raw["recovery_score"])

    # hrv, rhr
    if raw.get("hrv"):
        u.s3_hrv = float(raw["hrv"])
    if raw.get("hr_avg"):
        u.s3_rhr = int(raw["hr_avg"])

    logger.info(f"normalize_polar: vo2max={u.s3_vo2max} rec_daily={u.s3_recovery_daily} hrv={u.s3_hrv}")
    return u


def normalize_strava(raw: dict) -> UnifiedUserData:
    """Strava → UnifiedUserData. Агрегатор: нагрузка + VDOT из прогнозов.

    raw — dict из athlete_cache (predictions, CTL/ATL/TSB, load_48h).
    НЕ источник биометрии (восстановление, HRV).
    """
    u = UnifiedUserData(sources=["strava"])

    # vo2max из прогнозов (VDOT)
    predictions = raw.get("predictions") or {}
    dist_map = {"5km": 5000, "10km": 10000, "1 mile": 1609}
    for dist_name, dist_m in dist_map.items():
        p = predictions.get(dist_name)
        if not p:
            continue
        try:
            parts = p["time"].split(":")
            t_sec = (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                     if len(parts) == 3 else int(parts[0]) * 60 + int(parts[1]))
            vdot = _vdot_from_race(dist_m, t_sec)
            if vdot:
                u.s3_vo2max = vdot
                u.s3_zones  = _zones_from_vdot(vdot)
                break
        except Exception:
            continue

    # load_recent
    load = raw.get("load_48h")
    if load and load.get("sessions_48h", 0) > 0:
        suffer = load.get("suffer_48h", 0) or 0
        u.s3_load_recent = LoadRecent(
            sessions       = load.get("sessions_48h", 0),
            total_km       = load.get("total_km_48h", load.get("km_48h", 0)),
            last_hours_ago = load.get("last_activity_hours_ago"),
            intensity      = _intensity_from_load(suffer) if suffer > 0 else "unknown",
        )

    # load_chronic: CTL/ATL/TSB (эталон) + recovery_total
    tl = raw.get("training_load") or {}
    if tl:
        ctl = tl.get("ctl"); atl = tl.get("atl"); tsb = tl.get("tsb")
        if tsb is None and ctl is not None and atl is not None:
            tsb = round(float(ctl) - float(atl), 1)
        if ctl is not None or atl is not None:
            u.s3_load_chronic = LoadChronic(
                ctl=ctl, atl=atl, tsb=tsb, summary=tl.get("summary", _tsb_summary(tsb))
            )
            u.s3_recovery_total = tsb  # TSB = длительная усталость (шкала ±)

    logger.info(f"normalize_strava: vo2max={u.s3_vo2max} tsb={u.s3_recovery_total}")
    return u


# ── Merge ─────────────────────────────────────────────────────

def merge(parts: list[UnifiedUserData]) -> UnifiedUserData:
    """Объединяет данные нескольких сервисов.

    Для каждого поля берёт первое ненулевое из списка.
    Порядок в списке = приоритет (решается на слое потребления, не здесь).
    """
    if not parts:
        return UnifiedUserData()
    if len(parts) == 1:
        return parts[0]

    result = UnifiedUserData()
    result.sources = []
    for p in parts:
        result.sources.extend(p.sources)

    scalar_fields = [
        "s3_vo2max", "s3_lactate_threshold_pace", "s3_lactate_threshold_hr", "s3_zones",
        "s3_recovery_daily", "s3_recovery_total", "s3_hrv", "s3_hrv_baseline", "s3_rhr",
    ]
    for f in scalar_fields:
        for p in parts:
            val = getattr(p, f)
            if val is not None:
                setattr(result, f, val)
                break

    for p in parts:
        if p.s3_load_recent is not None:
            result.s3_load_recent = p.s3_load_recent
            break
    for p in parts:
        if p.s3_load_chronic is not None:
            result.s3_load_chronic = p.s3_load_chronic
            break

    result.updated_at = datetime.now(timezone.utc).isoformat()
    return result
