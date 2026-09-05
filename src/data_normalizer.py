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
    s3_recovery_daily:          int | None   = None  # 0–100, суточное (COROS/Polar, не Garmin/Whoop)
    s3_recovery_total:          float | None = None  # TSB Strava (форма ±), не TR
    s3_recovery_total_at:       str | None   = None  # ISO timestamp TSB
    s3_training_readiness:      dict | None  = None  # {"score": 0-100, "level": str, "factors": list}
    s3_training_readiness_at:   str | None   = None  # ISO timestamp TR
    s3_body_battery:            int | None   = None  # Garmin Body Battery, 0-100 (НЕ для промпта)
    s3_body_battery_at:         str | None   = None  # lastSyncTimestampGMT
    s3_coros_recovery:          int | None   = None  # COROS recoveryPct, 0-100 (НЕ для промпта)
    s3_hrv:                     float | None = None  # мс, ночной
    s3_hrv_at:                  str | None   = None  # ISO timestamp HRV
    s3_hrv_baseline:            float | None = None  # 7-дневная база
    s3_rhr:                     int | None   = None  # ЧСС покоя

    # Сон
    s3_sleep_hours: float | None = None  # часов сна
    s3_sleep_score: int | None   = None  # качество 0–100

    # Нагрузка
    s3_load_recent:  LoadRecent | None  = None
    s3_load_chronic: LoadChronic | None = None

    # Мета
    sources:    list = field(default_factory=list)
    updated_at: str  = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Даты фиксации: когда каждый метрик был записан/получен
    # Ключи: '<service>_fetched' (время fetch_raw), '<service>_measured' (фактическая дата измерения)
    # Пример: {'garmin_fetched': '2026-06-03T03:45:12', 'polar_measured': '2026-06-03'}
    data_dates: dict = field(default_factory=dict)

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
    # Body Battery — хранится отдельно, НЕ используется в промпте рекомендации
    bb = raw.get("body_battery")
    if bb is not None:
        u.s3_body_battery = int(bb)
    bb_at = raw.get("garmin_synced_at")
    if bb_at:
        u.s3_body_battery_at = bb_at
    # Training Readiness — только в своё поле, НЕ в recovery_total (TSB ≠ TR)
    tr = raw.get("training_readiness") or {}
    if isinstance(tr, dict) and tr.get("score") is not None:
        u.s3_training_readiness = tr
        tr_at = raw.get("training_readiness_at")
        if tr_at:
            u.s3_training_readiness_at = tr_at
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
    hrv_at = raw.get("hrv_at")
    if hrv_at:
        u.s3_hrv_at = hrv_at
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

    # Сон
    sleep_h = raw.get("sleep_hours") or raw.get("sleep_duration")
    if sleep_h is not None:
        try:
            u.s3_sleep_hours = round(float(sleep_h), 2)
        except Exception:
            pass
    sleep_s = raw.get("sleep_score") or raw.get("sleep_quality")
    if sleep_s is not None:
        try:
            u.s3_sleep_score = int(sleep_s)
        except Exception:
            pass

    logger.info(f"normalize_garmin: vo2max={u.s3_vo2max} lt={u.s3_lactate_threshold_pace} "
                f"rec_daily={u.s3_recovery_daily} rec_total={u.s3_recovery_total} hrv={u.s3_hrv} "
                f"sleep_h={u.s3_sleep_hours} sleep_score={u.s3_sleep_score}")
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
    # recoveryPct — суточное, хранится отдельно, НЕ подаётся в промпт
    if raw.get("recovery_score") is not None:
        u.s3_coros_recovery = int(raw["recovery_score"])
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


def normalize_coros_mcp(raw: dict) -> UnifiedUserData:
    """COROS по новой схеме (MCP) → UnifiedUserData.

    raw — плоский набор полей из coros_mcp.parse_raw.
    Отдельно от normalize_coros: там свой формат и свои поля.
    """
    u = UnifiedUserData(sources=["coros_mcp"])

    # ── Тренированность ──
    if raw.get("vo2max"):
        u.s3_vo2max = round(float(raw["vo2max"]), 1)

    ltsp_sec = raw.get("threshold_pace_sec")
    if ltsp_sec:
        u.s3_lactate_threshold_pace = _sec_to_pace(int(ltsp_sec))
        if not u.s3_vo2max:
            u.s3_vo2max = _vdot_from_ltsp(int(ltsp_sec))
        vdot = _vdot_from_ltsp(int(ltsp_sec))
        if vdot:
            u.s3_zones = _zones_from_vdot(vdot)

    # ── Восстановление ──
    # Суточный процент; пустое значение разборщик уже отсек — сюда приходит None.
    if raw.get("recovery_pct") is not None:
        u.s3_coros_recovery = int(raw["recovery_pct"])

    if raw.get("hrv"):
        u.s3_hrv = float(raw["hrv"])
    if raw.get("hrv_baseline"):
        u.s3_hrv_baseline = float(raw["hrv_baseline"])
    if raw.get("rhr"):
        u.s3_rhr = int(raw["rhr"])

    # ── Сон ──
    if raw.get("sleep_hours"):
        u.s3_sleep_hours = float(raw["sleep_hours"])
    if raw.get("sleep_score"):
        u.s3_sleep_score = int(raw["sleep_score"])

    # ── Нагрузка за 48 часов ──
    if raw.get("sessions_48h"):
        u.s3_load_recent = LoadRecent(
            sessions       = raw["sessions_48h"],
            total_km       = raw.get("total_km_48h", 0),
            last_hours_ago = raw.get("last_activity_hours_ago"),
            intensity      = _intensity_from_pace_vs_lt(None, u.s3_lactate_threshold_pace),
        )

    # ── Нагрузка ──
    # COROS отдаёт длинную и короткую нагрузку; форму считаем сами как их разность.
    ctl = raw.get("ctl")
    atl = raw.get("atl")
    if ctl is not None or atl is not None:
        ctl_v = float(ctl) if ctl is not None else None
        atl_v = float(atl) if atl is not None else None
        tsb = round(ctl_v - atl_v, 1) if ctl_v is not None and atl_v is not None else None
        u.s3_load_chronic = LoadChronic(ctl=ctl_v, atl=atl_v, tsb=tsb,
                                        summary=_tsb_summary(tsb))

    if raw.get("date"):
        u.data_dates["coros_mcp_measured"] = raw["date"]

    logger.info(f"normalize_coros_mcp: vo2max={u.s3_vo2max} lt={u.s3_lactate_threshold_pace} "
                f"ctl={ctl} atl={atl} rec={u.s3_coros_recovery}")
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

    # hrv, rhr, hrv_at
    if raw.get("hrv"):
        u.s3_hrv = float(raw["hrv"])
    if raw.get("hr_avg"):
        u.s3_rhr = int(raw["hr_avg"])
    if raw.get("hrv_at"):
        u.s3_hrv_at = raw["hrv_at"]

    # сон
    if raw.get("sleep_hours") is not None:
        u.s3_sleep_hours = float(raw["sleep_hours"])
    if raw.get("sleep_score") is not None:
        u.s3_sleep_score = int(raw["sleep_score"])

    logger.info(f"normalize_polar: vo2max={u.s3_vo2max} rec_daily={u.s3_recovery_daily} "
                f"hrv={u.s3_hrv} sleep_h={u.s3_sleep_hours} sleep_score={u.s3_sleep_score}")
    return u


def normalize_whoop(raw: dict) -> UnifiedUserData:
    """Whoop → UnifiedUserData. Источник данных за прошлую ночь: HRV + ЧСС покоя + сон.

    raw — dict из _parse_whoop_raw: {hrv, rhr, sleep_score, sleep_hours}.
    Не источник тренированности/нагрузки/суточного восстановления.
    """
    u = UnifiedUserData(sources=["whoop"])

    # HRV, RHR (ночные)
    if raw.get("hrv"):
        u.s3_hrv = float(raw["hrv"])
    if raw.get("rhr"):
        u.s3_rhr = int(round(float(raw["rhr"])))
    # Сон
    if raw.get("sleep_hours") is not None:
        u.s3_sleep_hours = round(float(raw["sleep_hours"]), 2)
    if raw.get("sleep_score") is not None:
        u.s3_sleep_score = int(round(float(raw["sleep_score"])))

    logger.info(f"normalize_whoop: hrv={u.s3_hrv} rhr={u.s3_rhr} "
                f"sleep_h={u.s3_sleep_hours} sleep_score={u.s3_sleep_score}")
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
        "s3_recovery_daily", "s3_recovery_total", "s3_recovery_total_at",
        "s3_training_readiness", "s3_training_readiness_at",
        "s3_body_battery", "s3_body_battery_at", "s3_coros_recovery",
        "s3_hrv", "s3_hrv_at", "s3_hrv_baseline", "s3_rhr",
        "s3_sleep_hours", "s3_sleep_score",
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


# ── Парсеры сырых API-ответов (мост между слоями 1.1 и 2) ─────

def _parse_garmin_raw(raw: dict) -> dict:
    """Извлекает нужные поля из сырого ответа Garmin API в формат для normalize_garmin.

    raw — dict из garmin.fetch_raw: {max_metrics, training_status, training_readiness,
                                      user_summary, hrv_data, lactate_threshold,
                                      activities_48h, user_profile}.
    """
    out: dict = {}

    # VO2max из max_metrics
    mm = raw.get("max_metrics")
    if isinstance(mm, list) and mm:
        g = (mm[0] or {}).get("generic") or {}
        vo2 = g.get("vo2MaxPreciseValue") or g.get("vo2MaxValue")
        if vo2:
            out["vo2max"] = round(float(vo2), 1)

    # Лактатный порог из lactate_threshold
    lt = raw.get("lactate_threshold")
    if isinstance(lt, dict):
        shr = lt.get("speed_and_heart_rate") or {}
        speed_raw = shr.get("speed")
        hr = shr.get("heartRate")
        if speed_raw and hr:
            try:
                speed_ms = float(speed_raw) * 10
                pace_sec = 1000 / speed_ms
                out["lactate_threshold_pace"] = f"{int(pace_sec) // 60}:{int(pace_sec) % 60:02d}"
                out["lactate_threshold_hr"] = int(hr)
            except Exception:
                pass

    # Body Battery, ЧСС покоя и метка последней синхронизации из user_summary
    us = raw.get("user_summary") or {}
    if isinstance(us, dict):
        bb = us.get("bodyBatteryMostRecentValue")
        if bb is not None:
            out["body_battery"] = int(bb)
        rhr = us.get("restingHeartRate")
        if rhr:
            out["rhr"] = int(rhr)
        sync_ts = us.get("lastSyncTimestampGMT")
        if sync_ts:
            out["garmin_synced_at"] = str(sync_ts)

    # Training Readiness (всегда список — берём первый элемент)
    tr_raw = raw.get("training_readiness")
    if tr_raw:
        item = tr_raw[0] if isinstance(tr_raw, list) else tr_raw
        if isinstance(item, dict) and item.get("score") is not None:
            level_raw = (item.get("level") or "").upper()
            level_map = {"POOR": "low", "LOW": "low", "FAIR": "low",
                         "MODERATE": "moderate", "GOOD": "moderate",
                         "HIGH": "high", "PRIME": "high", "OPTIMAL": "high"}
            out["training_readiness"] = {
                "score": item["score"],
                "level": level_map.get(level_raw, level_raw.lower() or "unknown"),
                "factors": [],
            }
            # timestamp самой метрики TR (UTC)
            tr_ts = item.get("timestamp")
            if tr_ts:
                out["training_readiness_at"] = str(tr_ts)

    # HRV из hrv_data
    hrv_raw = raw.get("hrv_data")
    if isinstance(hrv_raw, dict):
        summary = hrv_raw.get("hrvSummary") or {}
        last_night = summary.get("lastNightAvg")
        weekly = summary.get("weeklyAvg")
        if last_night or weekly:
            out["hrv"] = {
                "hrv_last_night": float(last_night) if last_night else None,
                "hrv_weekly_avg": float(weekly) if weekly else None,
            }
        # timestamp HRV если есть
        hrv_ts = summary.get("lastNightFiveMinAvgEndTimestampGMT") or summary.get("endTimestampGMT")
        if hrv_ts:
            out["hrv_at"] = str(hrv_ts)

    # Training Load из training_status
    ts = raw.get("training_status")
    if ts:
        item = ts[0] if isinstance(ts, list) else ts
        if isinstance(item, dict):
            acute = item.get("acuteLoad")
            chronic = item.get("chronicLoad")
            if acute is not None or chronic is not None:
                a = float(acute) if acute is not None else None
                c = float(chronic) if chronic is not None else None
                tsb = round(c - a, 1) if (a is not None and c is not None) else None
                out["training_load"] = {"ctl": c, "atl": a, "tsb": tsb}

    # Нагрузка за 48ч из activities_48h
    acts = raw.get("activities_48h")
    if isinstance(acts, list) and acts:
        total_km = round(sum((a.get("distance") or 0) for a in acts) / 1000, 1)
        out["load_48h"] = {"sessions_48h": len(acts), "total_km_48h": total_km, "km_48h": total_km}

    # Сон из sleep_data (если endpoint включён в fetch_raw)
    sleep_data = raw.get("sleep_data")
    if isinstance(sleep_data, dict):
        secs = sleep_data.get("sleepTimeSeconds") or sleep_data.get("sleepingSeconds")
        if secs:
            out["sleep_hours"] = round(int(secs) / 3600, 2)
        score = sleep_data.get("overallSleepScore") or sleep_data.get("sleepScoreTotal")
        if score is not None:
            out["sleep_score"] = int(score)
    # Fallback: сон из user_summary (Garmin иногда даёт sleepingSeconds там)
    if "sleep_hours" not in out and isinstance(us, dict):
        secs = us.get("sleepingSeconds") or us.get("sleepDurationSeconds")
        if secs:
            out["sleep_hours"] = round(int(secs) / 3600, 2)
        score = us.get("averageSleepStress")
        if score is not None and "sleep_score" not in out:
            out["sleep_score"] = max(0, min(100, round(100 - float(score))))

    return out


def _parse_coros_raw(raw: dict) -> dict:
    """Извлекает нужные поля из сырого ответа COROS API в формат для normalize_coros.

    raw — dict из coros.fetch_raw: {dashboard, account, activity}.
    """
    out: dict = {}

    dash = raw.get("dashboard")
    if isinstance(dash, dict) and str(dash.get("result")) == "0000":
        info = ((dash.get("data") or {}).get("summaryInfo")) or {}

        # Лактатный порог
        ltsp = info.get("ltsp")
        lthr = info.get("lthr")
        if ltsp and int(ltsp) > 0:
            s = int(ltsp)
            out["lactate_threshold_pace"] = f"{s // 60}:{s % 60:02d}"
            out["ltsp"] = s
        if lthr and int(lthr) > 0:
            out["lactate_threshold_hr"] = int(lthr)

        # Recovery (суточное)
        rpc = info.get("recoveryPct")
        if rpc is not None:
            out["recovery_score"] = max(0, min(100, int(rpc)))

        # HRV
        hrv_data = info.get("sleepHrvData") or {}
        if isinstance(hrv_data, dict):
            hrv_last = hrv_data.get("avgSleepHrv")
            hrv_base = hrv_data.get("sleepHrvBase")
            if hrv_last and float(hrv_last) > 0:
                out["hrv"] = float(hrv_last)
            if hrv_base and float(hrv_base) > 0:
                out["hrv_baseline"] = float(hrv_base)

        # RHR
        rhr = info.get("rhr")
        if rhr and int(rhr) > 0:
            out["rhr"] = int(rhr)

        # VO2max
        for key in ("vo2Max", "vo2max", "maxOxygenUptake"):
            val = info.get(key)
            if val and float(val) > 0:
                out["vo2max"] = round(float(val), 1)
                break

    # Нагрузка 48ч из activity
    activity = raw.get("activity")
    if isinstance(activity, dict) and str(activity.get("result")) == "0000":
        acts_list = ((activity.get("data") or {}).get("dataList")) or []
        if acts_list:
            total_km = round(sum((a.get("distance") or 0) for a in acts_list if isinstance(a, dict)) / 1000, 1)
            out["load_48h"] = {"sessions_48h": len(acts_list), "total_km_48h": total_km}

    # ati/cti (родной COROS Form) из /analyse/query (ключ "analyse", data.dayList) — ТОЛЬКО за сегодня (МСК).
    # Старые дни не берём: лучше без TSB, чем с протухшим. Запись без даты тоже не берём.
    dd = raw.get("analyse")
    if isinstance(dd, dict) and str(dd.get("result")) == "0000":
        items = ((dd.get("data") or {}).get("dayList")) or []
        items = [i for i in items if isinstance(i, dict)]
        from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
        _today_compact = _dt2.now(_tz2(_td2(hours=3))).strftime("%Y%m%d")

        def _day_of(it):
            for k in ("happenDay", "day", "date"):
                v = it.get(k)
                if v is not None:
                    return str(v)
            return None

        for item in reversed(items):
            ati = item.get("ati")
            cti = item.get("cti")
            if ati is None and cti is None:
                continue
            d = _day_of(item)
            if d != _today_compact:
                continue  # не сегодняшний день или нет даты — пропускаем
            if ati is not None:
                out["ati"] = float(ati)
            if cti is not None:
                out["cti"] = float(cti)
            break

    return out


def _parse_polar_raw(raw: dict) -> dict:
    """Извлекает нужные поля из сырого ответа Polar API в формат для normalize_polar.

    raw — dict из polar.fetch_raw: {profile, nightly_recharge, sleep}.
    """
    out: dict = {}

    # VO2max из profile
    profile = raw.get("profile")
    if isinstance(profile, dict):
        for key in ("vo2max", "vo2Max", "aerobic-fitness", "aerobicFitness"):
            val = profile.get(key)
            if val:
                try:
                    fval = float(val)
                    if fval > 10:
                        out["vo2max"] = round(fval, 1)
                        break
                except Exception:
                    pass

    # ANS Recharge / HRV / RHR из nightly_recharge
    recharge = raw.get("nightly_recharge")
    items: list = []
    if isinstance(recharge, list):
        items = recharge
    elif isinstance(recharge, dict):
        items = recharge.get("recharges") or recharge.get("items") or []
    items = [x for x in items if isinstance(x, dict)]
    if items:
        items.sort(key=lambda x: x.get("date", ""))
        last = items[-1]
        ans = last.get("ans_charge")
        if ans is not None:
            out["ans_charge"] = float(ans)
        hrv = last.get("heart_rate_variability_avg")
        if hrv is not None:
            out["hrv"] = round(float(hrv), 1)
        hr_avg = last.get("heart_rate_avg")
        if hr_avg is not None:
            out["hr_avg"] = int(float(hr_avg))

    # Сон из sleep-блока
    sleep_raw = raw.get("sleep")
    nights: list = []
    if isinstance(sleep_raw, list):
        nights = sleep_raw
    elif isinstance(sleep_raw, dict):
        nights = sleep_raw.get("nights") or sleep_raw.get("items") or []
    nights = [x for x in nights if isinstance(x, dict)]
    if nights:
        nights.sort(key=lambda x: x.get("date", ""))
        last_night = nights[-1]
        # sleep_end_time → hrv_at (метка ночных метрик)
        sleep_end = last_night.get("sleep_end_time")
        if sleep_end:
            out["hrv_at"] = str(sleep_end)
        # sleep_score
        score = last_night.get("sleep_score")
        if score is not None:
            out["sleep_score"] = int(score)
        # sleep_hours из start/end timestamps
        start = last_night.get("sleep_start_time")
        if start and sleep_end:
            try:
                from datetime import datetime as _dt
                _fmt = "%Y-%m-%dT%H:%M:%S.%f%z"
                def _parse_ts(s):
                    # Polar даёт tz как +03:00 — fromisoformat справляется
                    return _dt.fromisoformat(s)
                dur_h = round((_parse_ts(sleep_end) - _parse_ts(start)).total_seconds() / 3600, 2)
                if 0 < dur_h < 24:
                    out["sleep_hours"] = dur_h
            except Exception:
                pass

    return out


def _parse_whoop_raw(raw: dict) -> dict:
    """Извлекает поля из сырого ответа Whoop API в формат для normalize_whoop.

    raw — dict из whoop.fetch_raw: {recovery: {records:[...]}, sleep: {records:[...]}}.
    Берёт records[0].score.
    """
    out: dict = {}

    rec = raw.get("recovery")
    if isinstance(rec, dict):
        records = rec.get("records") or []
        if records:
            r0 = records[0] or {}
            score = r0.get("score") or {}
            hrv = score.get("hrv_rmssd_milli")
            if hrv is not None:
                out["hrv"] = round(float(hrv), 1)
            rhr = score.get("resting_heart_rate")
            if rhr is not None:
                out["rhr"] = float(rhr)
            created = r0.get("created_at") or ""
            if created:
                out["recovery_date"] = created[:10]  # метка «за какую ночь»

    slp = raw.get("sleep")
    if isinstance(slp, dict):
        records = slp.get("records") or []
        if records:
            s0 = records[0] or {}
            score = s0.get("score") or {}
            sp = score.get("sleep_performance_percentage")
            if sp is not None:
                out["sleep_score"] = float(sp)
            stage = score.get("stage_summary") or {}
            total_ms = (
                (stage.get("total_light_sleep_time_milli") or 0) +
                (stage.get("total_slow_wave_sleep_time_milli") or 0) +
                (stage.get("total_rem_sleep_time_milli") or 0)
            )
            if total_ms:
                out["sleep_hours"] = round(total_ms / 3_600_000, 2)

    return out


def _parse_strava_raw(raw: dict, athlete_cache: dict | None = None) -> dict:
    """Извлекает нужные поля из сырого ответа Strava API в формат для normalize_strava.

    raw — dict из strava.fetch_raw: {athlete, activities}.
    athlete_cache — опциональный кэш (predictions, CTL/ATL/TSB) из БД.
    """
    from datetime import datetime as _dt, timedelta as _td
    out: dict = {}

    # Прогнозы из athlete_cache (рассчитаны refresh_athlete_cache)
    if athlete_cache:
        predictions = athlete_cache.get("predictions") or {}
        if predictions:
            out["predictions"] = predictions
        tl = athlete_cache.get("training_load") or {}
        if tl.get("ctl") is not None:
            out["training_load"] = tl
            tsb = tl.get("tsb")
            if tsb is not None:
                out["s3_recovery_total"] = tsb

    # Нагрузка 48ч из activities
    acts = raw.get("activities")
    if isinstance(acts, list):
        cutoff = _dt.utcnow() - _td(hours=48)
        recent = []
        for a in acts:
            if a.get("type") != "Run":
                continue
            ts = a.get("start_date", "")
            try:
                dt = _dt.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                if dt >= cutoff:
                    recent.append(a)
            except Exception:
                pass
        if recent:
            total_km = round(sum((a.get("distance") or 0) for a in recent) / 1000, 1)
            suffer = sum((a.get("suffer_score") or 0) for a in recent)
            out["load_48h"] = {
                "sessions_48h": len(recent),
                "total_km_48h": total_km,
                "suffer_48h": suffer,
            }

    return out


def _readiness_base(ctl, atl, tsb) -> float | None:
    """База готовности 0-100 для источников без родного Training Readiness.

    Считаем по ОТНОШЕНИЮ короткой нагрузки к длинной, а не по их разности:
    разность зависит от объёмов бегуна и от единиц сервиса, отношение — нет.
    Шкала: 0.8 → 100 (свежесть после отдыха), ~1.15 → 50 (обычная неделя),
    1.5 → 0 (перегрузка). Без нагрузок — старый расчёт от формы.
    """
    try:
        if ctl and atl and float(ctl) > 0:
            ratio = float(atl) / float(ctl)
            return max(0.0, min(100.0, (1.5 - ratio) / 0.7 * 100.0))
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    if tsb is None:
        return None
    return max(0.0, min(100.0, (float(tsb) + 20.0) / 0.4))


def run_normalization(user_id: int) -> "UnifiedUserData | None":
    """Слой 2: читает raw_service_data из БД → нормализует → мёрджит → сохраняет в unified_cache.

    Приоритет при мёрдже (по DATA_NORMALIZER_SPEC.md):
    - тренированность: garmin → coros → polar → strava
    - восстановление суточное: coros → polar → garmin
    - нагрузка: strava → garmin → coros
    """
    import json as _json
    import database as db

    u_garmin = u_coros = u_coros_mcp = u_polar = u_strava = u_whoop = None

    # Даты фиксации для каждого сервиса
    data_dates: dict = {}

    # Garmin
    raw_g = db.get_raw_service_data(user_id, "garmin")
    if raw_g:
        data_dates["garmin_fetched"] = raw_g["fetched_at"]
        try:
            parsed = _parse_garmin_raw(_json.loads(raw_g["raw_json"]))
            u_garmin = normalize_garmin(parsed)
            if parsed.get("garmin_synced_at"):
                data_dates["garmin_synced_at"] = parsed["garmin_synced_at"]
        except Exception as e:
            logger.warning(f"normalize_garmin error user={user_id}: {e}")

    # COROS
    raw_c = db.get_raw_service_data(user_id, "coros")
    if raw_c:
        data_dates["coros_fetched"] = raw_c["fetched_at"]
        try:
            parsed = _parse_coros_raw(_json.loads(raw_c["raw_json"]))
            u_coros = normalize_coros(parsed)
        except Exception as e:
            logger.warning(f"normalize_coros error user={user_id}: {e}")

    # COROS по новой схеме (MCP) — отдельный источник, свой формат
    raw_cm = db.get_raw_service_data(user_id, "coros_mcp")
    if raw_cm:
        data_dates["coros_mcp_fetched"] = raw_cm["fetched_at"]
        try:
            import coros_mcp as _cm
            parsed = _cm.parse_raw(_json.loads(raw_cm["raw_json"]))
            u_coros_mcp = normalize_coros_mcp(parsed)
            if parsed.get("date"):
                data_dates["coros_mcp_measured"] = parsed["date"]
        except Exception as e:
            logger.warning(f"normalize_coros_mcp error user={user_id}: {e}")

    # Polar
    raw_p = db.get_raw_service_data(user_id, "polar")
    if raw_p:
        data_dates["polar_fetched"] = raw_p["fetched_at"]
        try:
            raw_p_data = _json.loads(raw_p["raw_json"])
            # Фактическая дата измерения восстановления (Polar явно даёт дату ночи)
            nr = raw_p_data.get("nightly_recharge")
            items = nr if isinstance(nr, list) else ((nr or {}).get("recharges") or (nr or {}).get("items") or [])
            items = [x for x in items if isinstance(x, dict)]
            if items:
                items.sort(key=lambda x: x.get("date", ""))
                mdate = items[-1].get("date")
                if mdate:
                    data_dates["polar_measured"] = str(mdate)
            parsed = _parse_polar_raw(raw_p_data)
            u_polar = normalize_polar(parsed)
        except Exception as e:
            logger.warning(f"normalize_polar error user={user_id}: {e}")

    # Strava (обогащаем athlete_cache для CTL/predictions)
    raw_s = db.get_raw_service_data(user_id, "strava")
    if raw_s:
        data_dates["strava_fetched"] = raw_s["fetched_at"]
        try:
            athlete_cache = db.get_athlete_cache(user_id)
            if athlete_cache and athlete_cache.get("updated_at"):
                data_dates["strava_ctl_at"] = athlete_cache["updated_at"]
            parsed = _parse_strava_raw(_json.loads(raw_s["raw_json"]), athlete_cache)
            u_strava = normalize_strava(parsed)
        except Exception as e:
            logger.warning(f"normalize_strava error user={user_id}: {e}")

    # Whoop
    raw_w = db.get_raw_service_data(user_id, "whoop")
    if raw_w:
        data_dates["whoop_fetched"] = raw_w["fetched_at"]
        try:
            parsed = _parse_whoop_raw(_json.loads(raw_w["raw_json"]))
            u_whoop = normalize_whoop(parsed)
            if parsed.get("recovery_date"):
                data_dates["whoop_measured"] = parsed["recovery_date"]
        except Exception as e:
            logger.warning(f"normalize_whoop error user={user_id}: {e}")

    if not any([u_garmin, u_coros, u_coros_mcp, u_polar, u_strava, u_whoop]):
        logger.info(f"run_normalization: нет сырых данных для user={user_id}")
        return None

    # Мёрдж: тренированность — garmin → coros_mcp → coros → polar → strava
    # Новый COROS выше старого: там родное измерение VO2max и порога,
    # у старого — расчёт из порога.
    fitness_priority = [p for p in [u_garmin, u_coros_mcp, u_coros, u_polar, u_strava] if p]
    merged = merge(fitness_priority)

    # recovery_daily: универсальное суточное восстановление 0-100.
    # Приоритет источников: Garmin (Body Battery) → COROS (recoveryPct) → Polar (ANS).
    # Один человек = одно устройство, конфликта нет.
    for val in (
        (u_garmin.s3_body_battery if u_garmin else None),
        (u_coros_mcp.s3_coros_recovery if u_coros_mcp else None),
        (u_coros.s3_coros_recovery if u_coros else None),
        (u_polar.s3_recovery_daily if u_polar else None),
    ):
        if val is not None:
            merged.s3_recovery_daily = val
            break

    # HRV/RHR/сон: если есть Whoop — приоритет ему (носимый ночью)
    if u_whoop:
        if u_whoop.s3_hrv is not None:
            merged.s3_hrv = u_whoop.s3_hrv
        if u_whoop.s3_rhr is not None:
            merged.s3_rhr = u_whoop.s3_rhr
        if u_whoop.s3_sleep_hours is not None:
            merged.s3_sleep_hours = u_whoop.s3_sleep_hours
        if u_whoop.s3_sleep_score is not None:
            merged.s3_sleep_score = u_whoop.s3_sleep_score
        if "whoop" not in merged.sources:
            merged.sources.append("whoop")

    # recovery_total: только Strava TSB (форма ±), TR отдельно в s3_training_readiness
    if u_strava and u_strava.s3_recovery_total is not None:
        merged.s3_recovery_total = u_strava.s3_recovery_total

    # Расчётный TR для COROS/Strava (у них нет нативного Training Readiness).
    # СТРОГО: считаем ТОЛЬКО при наличии сегодняшнего TSB (COROS ati/cti или Strava).
    # base = clip((TSB+20)/0.4, 0..100). COROS: TR = sqrt(base * RecoveryPct);
    # Strava-only: TR = base. БЕЗ TSB TR не считаем — один Recovery не TR.
    # Garmin-юзеров не трогаем: родной TR приоритетен, Garmin BB в расчёт НЕ идёт.
    _tr_obj = merged.s3_training_readiness
    _has_native_tr = isinstance(_tr_obj, dict) and _tr_obj.get("score") is not None
    # Из двух COROS берём тот, у кого форма реально есть: у старого она чаще пустая.
    _c_any = next(
        (c for c in (u_coros_mcp, u_coros)
         if c and c.s3_load_chronic and c.s3_load_chronic.tsb is not None),
        u_coros_mcp or u_coros,
    )
    if not _has_native_tr and not u_garmin:
        _tsb = merged.s3_recovery_total
        # Родной COROS Form приоритетнее Strava TSB
        if (_c_any and _c_any.s3_load_chronic
                and _c_any.s3_load_chronic.tsb is not None):
            _tsb = _c_any.s3_load_chronic.tsb
        if _tsb is not None:
            _lc = (_c_any.s3_load_chronic if (_c_any and _c_any.s3_load_chronic)
                   else merged.s3_load_chronic)
            _base = _readiness_base(
                getattr(_lc, "ctl", None), getattr(_lc, "atl", None), _tsb)
        if _tsb is not None and _base is not None:
            _coros_rec = _c_any.s3_coros_recovery if _c_any else None
            if _coros_rec is not None:
                _calc, _src = (_base * float(_coros_rec)) ** 0.5, "coros-calc"
            else:
                _calc, _src = _base, "strava-calc"
            _score = int(round(max(0.0, min(100.0, _calc))))
            merged.s3_training_readiness = {"score": _score, "level": _src, "factors": {}}
            logger.info(f"run_normalization: расчётный TR={_score} ({_src}, tsb={_tsb}) user={user_id}")

    # load_recent: strava → garmin → coros
    for p in [u_strava, u_garmin, u_coros, u_coros_mcp]:
        if p and p.s3_load_recent is not None:
            merged.s3_load_recent = p.s3_load_recent
            break

    # load_chronic: strava → garmin → coros_mcp → coros
    for p in [u_strava, u_garmin, u_coros_mcp, u_coros]:
        if p and p.s3_load_chronic is not None:
            merged.s3_load_chronic = p.s3_load_chronic
            break

    sources = list(dict.fromkeys(merged.sources))  # deduplicate, preserve order
    merged.sources = sources
    merged.data_dates = data_dates

    db.save_unified_data(user_id, merged.to_json(), ",".join(sources))
    logger.info(
        f"run_normalization OK: user={user_id} sources={sources} "
        f"vo2max={merged.s3_vo2max} lt={merged.s3_lactate_threshold_pace} "
        f"rec_daily={merged.s3_recovery_daily} hrv={merged.s3_hrv}"
    )
    return merged
