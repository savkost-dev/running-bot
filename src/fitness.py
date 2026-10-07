"""Данные физической формы (fitness) и кэш атлета.

Вынесено из bot.py без изменения логики. Чистый лист: тянет только из
database/сервисов/strava, обратных импортов в bot.py не имеет.

Функции:
- refresh_athlete_cache      — обновляет кэш CTL/ATL/TSB, прогнозы, соревнования (Strava)
- get_fitness_data           — fitness из Strava (быстрые + кэш медленных)
- get_garmin_fitness_data    — fitness из Garmin Connect
- get_coros_fitness_data     — fitness из COROS
- get_polar_fitness_data     — fitness из Polar
- _get_vo2max_from_tracker   — VO2max из первого доступного трекера
- get_last_run / idle_days / get_idle — последняя пробежка, дни простоя, сдвиг зон (02–05.10.2026)
"""
import asyncio
import logging
from datetime import date

from database import get_token, save_athlete_cache, get_athlete_cache, get_strava_activities
from strava import get_full_athlete_data

logger = logging.getLogger(__name__)


# ── ПОСЛЕДНЯЯ ПРОБЕЖКА И ПРОСТОЙ (02.10.2026, Антон) ──────────
# Зачем: рекомендация строится от VO2max и порога, которые за несколько дней не меняются,
# а работа после 3–4 дней полного отдыха идёт заметно хуже («двигатель на холодную»:
# падает объём плазмы, обмен уходит в углеводы). Первый из трёх уровней поправок
# (простой → большая нагрузка → опорно-двигательный), остальные два — отдельными шагами.
#
# Откуда дата: из сырья ночной загрузки каждого трекера (raw_service_data), где теперь лежит
# список тренировок за LAST_RUN_WINDOW_DAYS — Garmin activities_14d, COROS querySportRecords,
# Polar exercises; разбирают его модули трекеров (garmin.last_run_date_from_activities,
# coros_mcp.last_run_date_from_records, polar.last_run_date_from_exercises). Strava — из окна
# strava_activities (strava.last_run_date_from_window). Отдельного хранения дат нет,
# запросов к сервисам при чтении нет. get_last_run берёт самую свежую по всем источникам:
# пробежка, записанная только на одних часах, не должна давать ложный простой.

LAST_RUN_WINDOW_DAYS = 14   # окно списка тренировок у трекеров
RUN_MIN_M = 3000            # пробежка — от 3 км или от 20 минут (раскатка да, прогулка нет)
RUN_MIN_S = 1200
MAX_IDLE_DAYS = 30          # старше — «очень давно», не ошибка


def is_run(dist_m, dur_s) -> bool:
    """Запись тянет на пробежку: от RUN_MIN_M метров или от RUN_MIN_S секунд."""
    try:
        d = float(dist_m or 0)
    except (TypeError, ValueError):
        d = 0.0
    try:
        t = float(dur_s or 0)
    except (TypeError, ValueError):
        t = 0.0
    return d >= RUN_MIN_M or t >= RUN_MIN_S


SESSION_MIN_S = 1200        # 06.10.2026: не-беговая аэробная тренировка — от 20 минут


def is_session(dur_s) -> bool:
    """Не-беговая аэробная запись тянет на тренировку: от SESSION_MIN_S секунд."""
    try:
        return float(dur_s or 0) >= SESSION_MIN_S
    except (TypeError, ValueError):
        return False


def latest_date(dates) -> str | None:
    """Самая свежая 'YYYY-MM-DD' из списка (пустые пропускаются) или None."""
    dates = [d for d in dates if d]
    return max(dates) if dates else None


RAW_FRESH_DAYS = 3          # сырьё старше — подключение считаем мёртвым, источник не учитываем


def _raw_of(db_user_id: int, service: str) -> dict | None:
    """Сырьё ночной загрузки сервиса из raw_service_data как dict, или None.
    05.10.2026: только если подключение живое (есть токен) и сырьё свежее RAW_FRESH_DAYS —
    иначе у отключившихся и заблокировавших бота старое сырьё давало ложный простой «30 дней»."""
    import json
    from datetime import datetime, timedelta
    from database import get_raw_service_data
    if not get_token(db_user_id, service):
        return None
    row = get_raw_service_data(db_user_id, service)
    if not row or not row.get("raw_json"):
        return None
    try:
        fetched = datetime.strptime(str(row.get("fetched_at"))[:19], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() - fetched > timedelta(days=RAW_FRESH_DAYS):
            return None
    except (TypeError, ValueError):
        return None
    try:
        raw = json.loads(row["raw_json"])
    except (TypeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def get_last_run(db_user_id: int) -> dict | None:
    """Последняя пробежка и последняя аэробная тренировка любого вида по всем живым источникам:
    {"date": дата последней пробежки | None, "source", "all": {источник: дата пробежки},
     "active_date": дата последней тренировки из засчитываемых (бег, велосипед, плавание, беговые лыжи) | None,
     "active_kind": её вид по-русски, "empty": [источники со свежим списком без единой тренировки]}.
    06.10.2026 (Антон): паузу сбрасывают бег, велосипед, плавание и беговые лыжи; остальное — нет.
    None — ни один живой источник ничего не знает. Читает только базу."""
    import garmin as _g
    import coros_mcp as _cm
    import polar as _p
    from strava import sessions_from_window
    runs, empty, sessions = {}, [], []

    def take(src, sess, full):
        d = latest_date(s["date"] for s in sess if s["run"])
        if d:
            runs[src] = d
        if sess:
            sessions.extend(sess)
        elif full:
            empty.append(src)

    raw = _raw_of(db_user_id, "garmin")
    if raw:
        acts = raw.get("activities_14d")
        full = isinstance(acts, list)
        if not full:
            acts = raw.get("activities_48h")   # сырьё до 0.35.0 — только 48 часов
        take("garmin", _g.sessions_from_activities(acts), full)
    raw = _raw_of(db_user_id, _cm.SERVICE)
    if raw and raw.get("querySportRecords"):
        take("coros_mcp", _cm.sessions_from_records(_cm.parse_sport_records(raw["querySportRecords"])), True)
    raw = _raw_of(db_user_id, "polar")
    if raw and isinstance(raw.get("exercises"), list):
        take("polar", _p.sessions_from_exercises(raw["exercises"]), True)
    if get_token(db_user_id, "strava"):
        take("strava", sessions_from_window(db_user_id), False)
    if not runs and not empty and not sessions:
        return None
    src = max(runs, key=lambda k: runs[k]) if runs else None
    last = max(sessions, key=lambda s: s["date"] or "") if sessions else None
    return {"date": runs[src] if src else None, "source": src, "all": runs, "empty": empty,
            "active_date": last["date"] if last else None, "active_kind": last["kind"] if last else None}


def get_idle(db_user_id: int, workout_date: str | None) -> dict | None:
    """05.10.2026: простой перед тренировкой — {"days", "shift", "last_run", "last_active", "last_kind",
    "source", "until"}. until — дата, до которой считали (день, когда человек бежит работу).
    days — полные дни без аэробных тренировок до workout_date (06.10: любой вид, не только бег;
    ничего во всём окне → LAST_RUN_WINDOW_DAYS), shift — сдвиг зон по zones.idle_shift_sec.
    None — данных нет, правило выключено."""
    import zones as _z
    lr = get_last_run(db_user_id)
    if not lr:
        return None
    days = idle_days(lr["active_date"], workout_date) if lr["active_date"] else LAST_RUN_WINDOW_DAYS
    if days is None:
        return None
    return {"days": days, "shift": _z.idle_shift_sec(days), "last_run": lr["date"], "source": lr["source"],
            "last_active": lr["active_date"], "last_kind": lr["active_kind"],
            "until": str(workout_date)[:10] if workout_date else date.today().isoformat()}


# ── ФАКТ РАБОТЫ ЗА ПОСЛЕДНИЕ ДНИ (07.10.2026, Антон) ──────────
# Второй уровень поправок («тяжёлая работа накануне»), пока ТОЛЬКО СБОР И ПОКАЗ АДМИНУ:
# в рекомендацию не входит. Один источник — главный трекер (как у VO2max), секунды не важны,
# нужен факт работы в зонах. Читает только базу: сырьё ночной загрузки и окно Strava.
#   garmin    — список activities_14d: activityTrainingLoad, aerobic/anaerobicTrainingEffect,
#               trainingEffectLabel, hrTimeInZone_1..5 (сек) — всё уже в сырье
#   coros_mcp — activity_details (getActivityDetail по свежим пробежкам): нагрузка, два эффекта, фокус;
#               времени в зонах MCP не отдаёт (есть только в веб-кабинете)
#   strava    — окно strava_activities: suffer_score, dd_hr_zones (время в пульсовых зонах,
#               только у подписчиков Strava)
WORK_DAYS = 3
# 07.10.2026 (Антон): не-беговая тренировка считается РАБОТОЙ (а не просто сбросом паузы) от этих
# длительностей: плавание — от часа, велосипед и беговые лыжи — от двух часов («там это уже длительная»).
WORK_MIN_S = {"плавание": 3600, "велосипед": 7200, "лыжи": 7200}


def is_work_session(kind, dur_s) -> bool:
    """Не-беговая тренировка тянет на работу: длительность ≥ WORK_MIN_S[вид]."""
    try:
        t = float(dur_s or 0)
    except (TypeError, ValueError):
        t = 0.0
    return t >= WORK_MIN_S.get(kind, SESSION_MIN_S)


def get_recent_work(db_user_id: int, days: int = WORK_DAYS) -> dict | None:
    """Тренировки за последние days дней по главному трекеру — виды как у правила простоя
    (07.10.2026, Антон: бег is_run; плавание от часа, велосипед и лыжи от двух часов — is_work_session).
    {"source", "days", "items": [{date, kind, run, km, dur_s, load, aerobic_te, anaerobic_te, focus, zones_s}]}
    zones_s — [сек в зонах 1..5] или None. None — главного трекера нет или сырьё не живое."""
    from datetime import timedelta
    from database import primary_vo2max_tracker
    src = primary_vo2max_tracker(db_user_id)
    if src not in ("garmin", "coros_mcp"):
        src = "strava" if get_token(db_user_id, "strava") else None
    if not src:
        return None
    since = (date.today() - timedelta(days=days)).isoformat()
    items = []
    if src == "garmin":
        import garmin as _g
        raw = _raw_of(db_user_id, "garmin")
        if raw is None:
            return None
        for a in raw.get("activities_14d") or []:
            if not isinstance(a, dict):
                continue
            tk = (a.get("activityType") or {}).get("typeKey")
            kind = _g.aerobic_kind(tk)
            d = str(a.get("startTimeLocal") or a.get("startTimeGMT") or "")[:10]
            if not kind or d < since:
                continue
            run = _g.is_run_type(tk)
            dur = a.get("duration") or a.get("movingDuration")
            if not (is_run(a.get("distance"), dur) if run else is_work_session(kind, dur)):
                continue
            z = [a.get(f"hrTimeInZone_{i}") for i in range(1, 6)]
            items.append({"date": d, "kind": kind, "run": run, "km": (a.get("distance") or 0) / 1000.0,
                          "dur_s": dur,
                          "load": a.get("activityTrainingLoad"),
                          "aerobic_te": a.get("aerobicTrainingEffect"), "anaerobic_te": a.get("anaerobicTrainingEffect"),
                          "focus": a.get("trainingEffectLabel"),
                          "zones_s": z if any(v is not None for v in z) else None})
    elif src == "coros_mcp":
        import coros_mcp as _cm
        raw = _raw_of(db_user_id, _cm.SERVICE)
        if raw is None:
            return None
        for lid, det in (raw.get("activity_details") or {}).items():
            if not isinstance(det, dict) or (det.get("date") or "") < since:
                continue
            p = _cm.parse_activity_detail(det.get("text"))
            st = det.get("sport_type")
            items.append({"date": det.get("date"), "kind": det.get("kind") or _cm.AEROBIC_KINDS.get(st) or "бег",
                          "run": st in _cm.RUN_SPORT_TYPES, "km": (p.get("distance_m") or 0) / 1000.0,
                          "dur_s": p.get("duration_s"), "load": p.get("load"),
                          "aerobic_te": p.get("aerobic_te"), "anaerobic_te": p.get("anaerobic_te"),
                          "focus": p.get("focus"), "zones_s": None})
    else:
        from database import get_strava_activities
        from strava import _aerobic_kind_strava
        for a in get_strava_activities(db_user_id):
            kind = _aerobic_kind_strava(a)
            d = str(a.get("start_date_local") or a.get("start_date") or "")[:10]
            if not kind or d < since:
                continue
            run = kind == "бег"
            if not (is_run(a.get("distance"), a.get("moving_time")) if run else is_work_session(kind, a.get("moving_time"))):
                continue
            z = None
            for zi in a.get("dd_hr_zones") or []:
                if isinstance(zi, dict) and zi.get("type") == "heartrate":
                    z = [b.get("time") for b in (zi.get("distribution_buckets") or [])]
            items.append({"date": d, "kind": kind, "run": run, "km": (a.get("distance") or 0) / 1000.0,
                          "dur_s": a.get("moving_time"),
                          "load": a.get("suffer_score"), "aerobic_te": None, "anaerobic_te": None,
                          "focus": None, "zones_s": z})
    items.sort(key=lambda x: x.get("date") or "", reverse=True)
    return {"source": src, "days": days, "items": items}


def recent_work_lines(work: dict | None) -> list[str]:
    """Строки для админа: «2026-10-06: 9.4 км 43 мин · нагрузка 115 · ТЭ 4.1/2.7 · зоны 0/11/9/24/0 мин»."""
    out = []
    for it in (work or {}).get("items") or []:
        parts = [f"{it['km']:.1f} км" + (f" {int(it['dur_s']) // 60} мин" if it.get("dur_s") else "")]
        if it.get("kind") and it.get("kind") != "бег":
            parts[0] = f"{it['kind']} " + parts[0]
        if it.get("load") is not None:
            parts.append(f"нагрузка {int(round(float(it['load'])))}")
        if it.get("aerobic_te") is not None or it.get("anaerobic_te") is not None:
            _te = lambda v: f"{float(v):.1f}" if v is not None else "—"
            parts.append(f"ТЭ {_te(it.get('aerobic_te'))}/{_te(it.get('anaerobic_te'))}")
        if it.get("focus"):
            parts.append(str(it["focus"]).lower())
        if it.get("zones_s"):
            parts.append("зоны " + "/".join(str(int(round((v or 0) / 60))) for v in it["zones_s"]) + " мин")
        out.append(f"{it.get('date')}: " + " · ".join(parts))
    return out


# 07.10.2026 (Антон): таблица объёма по дням относительно даты тренировки — для админа и для ИИ
# («ИИ дальше сам разберётся»: формул и порогов нет). Строка — день: 0 = день тренировки, -1 = накануне…
# км/мин/нагрузка/зоны — суммы за день; ТЭ — максимум аэробного и анаэробного; работа — ярлыки часов.
WORK_TABLE_DAYS = 7
_LABEL_RU = {  # Garmin trainingEffectLabel и COROS Training Focus → по-русски
    "recovery": "восст.", "aerobic_base": "база", "base": "база", "tempo": "темповая",
    "lactate_threshold": "порог", "threshold": "порог", "vo2max": "МПК", "anaerobic_capacity": "анаэробная",
    "anaerobic": "анаэробная", "speed": "скорость", "sprint": "скорость", "unknown": "", "?": "", "none": "",
}


def label_ru(label) -> str:
    if not label:
        return ""
    key = str(label).strip().lower()
    return _LABEL_RU.get(key, key)


def work_days_table(db_user_id: int, workout_date: str | None = None, days: int = WORK_TABLE_DAYS) -> dict | None:
    """Дни от сегодня назад (07.10.2026, Антон: 0 = сегодня, -1 = вчера — рекомендация делается вечером
    накануне, строка 0 от даты тренировки была бы всегда пустой). workout_date — дата тренировки,
    в шапке «тренировка завтра / сегодня / через N дней» (work_when_text).
    rows: [{rel, date, km, min, load, aer, ana, z45_min, n, labels[], kinds[]}]; пустые дни — n=0.
    None — главного трекера нет / сырьё не живое."""
    from datetime import timedelta
    today = date.today()
    work = get_recent_work(db_user_id, days=days)
    if work is None:
        return None
    rows = {}
    for rel in range(0, -days, -1):
        d = (today + timedelta(days=rel)).isoformat()
        rows[d] = {"rel": rel, "date": d, "km": 0.0, "min": 0, "load": None, "aer": None, "ana": None,
                   "z45_min": None, "n": 0, "labels": [], "kinds": []}
    for it in work.get("items") or []:
        r = rows.get(it.get("date"))
        if not r:
            continue
        r["n"] += 1
        r["km"] += float(it.get("km") or 0)
        r["min"] += int(float(it.get("dur_s") or 0) // 60)
        if it.get("load") is not None:
            r["load"] = (r["load"] or 0.0) + float(it["load"])
        for k, v in (("aer", it.get("aerobic_te")), ("ana", it.get("anaerobic_te"))):
            if v is not None:
                r[k] = max(r[k], float(v)) if r[k] is not None else float(v)
        z = it.get("zones_s") or []
        if len(z) >= 5:
            r["z45_min"] = (r["z45_min"] or 0) + int(round(((z[3] or 0) + (z[4] or 0)) / 60))
        lab = label_ru(it.get("focus"))
        if lab:
            r["labels"].append(lab)
        if it.get("kind") and it.get("kind") != "бег":
            r["kinds"].append(it["kind"])
    wrel = None
    if workout_date:
        try:
            wrel = (date.fromisoformat(str(workout_date)[:10]) - today).days
        except ValueError:
            wrel = None
    return {"source": work["source"], "today": today.isoformat(), "days": days,
            "workout_date": str(workout_date)[:10] if workout_date else None, "workout_rel": wrel,
            "rows": [rows[k] for k in sorted(rows, reverse=True)]}


def work_when_text(tbl: dict | None) -> str:
    """«тренировка завтра» / «тренировка сегодня» / «тренировка через N дней» / прошла — «считаем как если бы сегодня» / ''."""
    wrel = (tbl or {}).get("workout_rel")
    if wrel is None:
        return ""
    if wrel == 0:
        return "тренировка сегодня"
    if wrel == 1:
        return "тренировка завтра"
    if wrel > 1:
        return f"тренировка через {wrel} {_days_word_ru(wrel)}"
    # 07.10.2026 (Антон): прошедшая тренировка считается как если бы она была сегодня (как у простоя)
    return "тренировка уже прошла — считаем как если бы она была сегодня"


def _days_word_ru(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def work_table_text(tbl: dict | None, zones: bool = True) -> str:
    """Моноширинная таблица: день км мин нагр ТЭ [з4+5] работа. Пустой день — прочерки."""
    if not tbl:
        return "нет данных (нет главного трекера или сырьё не живое)"
    hdr = f"{'день':>4} {'км':>5} {'мин':>4} {'нагр':>5} {'ТЭ':>7}" + (f" {'з4+5':>4}" if zones else "") + "  работа"
    out = [hdr]
    for r in tbl["rows"]:
        if not r["n"]:
            out.append(f"{r['rel']:>4} {'—':>5} {'—':>4} {'—':>5} {'—':>7}" + (f" {'—':>4}" if zones else "") + "  —")
            continue
        te = (f"{r['aer']:.1f}" if r["aer"] is not None else "—") + "/" + (f"{r['ana']:.1f}" if r["ana"] is not None else "—")
        work = ", ".join(dict.fromkeys(r["labels"])) or "—"
        if r["kinds"]:
            work = ", ".join(dict.fromkeys(r["kinds"])) + (f"; {work}" if work != "—" else "")
        if r["n"] > 1:
            work += f" ({r['n']} трен.)"
        load = f"{int(round(r['load']))}" if r["load"] is not None else "—"
        z45 = f"{r['z45_min']}" if r["z45_min"] is not None else "—"
        out.append(f"{r['rel']:>4} {r['km']:>5.1f} {r['min']:>4} {load:>5} {te:>7}"
                   + (f" {z45:>4}" if zones else "") + f"  {work}")
    return "\n".join(out)


def idle_days(last_run_date: str | None, workout_date: str | None) -> int | None:
    """Полные дни без бега между последней пробежкой и днём работы.
    Последняя в воскресенье, работа в пятницу → пн, вт, ср, чт = 4.
    Без даты пробежки — None (фактор выключен); старше MAX_IDLE_DAYS — MAX_IDLE_DAYS."""
    if not last_run_date:
        return None
    try:
        lr = date.fromisoformat(str(last_run_date)[:10])
        wd = date.fromisoformat(str(workout_date)[:10]) if workout_date else date.today()
    except ValueError:
        return None
    n = (wd - lr).days - 1
    if n < 0:
        return 0
    return min(n, MAX_IDLE_DAYS)


# ── КЭШ АТЛЕТА ───────────────────────────────────────────────

async def refresh_athlete_cache(db_user_id: int, access_token: str, notify_msg=None,
                                fill_window: bool = False) -> dict | None:
    """
    Обновляет кэш данных атлета (CTL/ATL/TSB, прогнозы, соревнования).
    Считается из окна strava_activities (90 дней), запросов к API нет.
    fill_window=True — сначала заполнить окно из API (список + деталь по каждой
    тренировке, 30-60 сек): только при подключении или по ручной команде.
    """
    try:
        if notify_msg:
            await notify_msg.edit_text(
                "⏳ Загружаю данные из Strava...\n"
                "Это займёт около минуты (только первый раз)"
            )

        # Страховка: окно пустое (первый запуск после деплоя, чистка БД) —
        # заполняем один раз, иначе CTL/ATL посчитаются по одной тренировке.
        if not fill_window and not get_strava_activities(db_user_id):
            logger.info(f"Окно strava_activities пусто для user_id={db_user_id} — заполняю")
            fill_window = True

        athlete_data = await get_full_athlete_data(access_token, db_user_id=db_user_id,
                                                   fill_window=fill_window)

        save_athlete_cache(
            db_user_id,
            athlete_data["training_load"],
            athlete_data["predictions"],
            athlete_data["last_race"]
        )

        logger.info(f"Кэш атлета обновлён для user_id={db_user_id}")
        return athlete_data

    except Exception as e:
        logger.error(f"Ошибка обновления кэша для user_id={db_user_id}: {e}")
        return None


async def get_fitness_data(db_user_id: int, access_token: str) -> dict | None:
    """
    Получает данные атлета:
    - Быстрые: пробежки за 14 дней + острая нагрузка за 48 ч — из окна
      strava_activities (обновляется вебхуком), без запросов к API
    - Медленные (из кэша): CTL/ATL/TSB, прогнозы Риегеля, последнее соревнование
    Нет кэша или окно пустое → разовое заполнение окна (fill_window) + пересчёт кэша.
    """
    from strava import get_recent_runs, analyze_fitness, get_recent_48h_load

    # ── Медленные данные (из кэша); при пустом окне — заполнить ──
    cache = get_athlete_cache(db_user_id)
    athlete_data = None
    if not cache or not get_strava_activities(db_user_id):
        logger.info(f"Кэш или окно Strava отсутствует для user_id={db_user_id}, заполняю...")
        athlete_data = await refresh_athlete_cache(db_user_id, access_token, fill_window=True)

    # ── Быстрые данные (из окна, 0 запросов к Strava API) ─────
    try:
        runs, load_48h = await asyncio.gather(
            get_recent_runs(access_token, days=14, db_user_id=db_user_id),
            get_recent_48h_load(access_token, db_user_id=db_user_id),
        )
        fitness = analyze_fitness(runs)
        fitness["load_48h"] = load_48h
    except Exception as e:
        logger.error(f"Ошибка получения пробежек: {e}")
        fitness = {"summary": "Нет данных", "total_km": 0, "run_count": 0,
                   "avg_pace": "—", "avg_hr": None, "fatigue_level": "unknown",
                   "load_48h": None}

    slow = athlete_data or cache
    if slow:
        fitness["training_load"] = slow["training_load"]
        fitness["predictions"]   = slow["predictions"]
        fitness["last_race"]     = slow["last_race"]

    return fitness


async def get_garmin_fitness_data(db_user_id: int) -> dict | None:
    """
    Получает данные атлета из Garmin Connect — аналог get_fitness_data() для Strava.
    Возвращает dict, совместимый со структурой fitness для промта.
    """
    import garmin as _garmin
    if not get_token(db_user_id, "garmin"):
        return None

    try:
        results = await asyncio.gather(
            _garmin.get_training_load(db_user_id),
            _garmin.get_activities_48h(db_user_id),
            _garmin.get_last_race(db_user_id),
            _garmin.get_best_efforts(db_user_id),
            return_exceptions=True,
        )
    except Exception as e:
        logger.error(f"Garmin fitness data error for {db_user_id}: {e}")
        return None

    training_load, activities_48h, last_race, best_efforts = results

    fitness = {
        "source": "garmin",
        "summary": "",
        "total_km": 0,
        "run_count": 0,
        "avg_pace": "—",
        "avg_hr": None,
        "fatigue_level": "unknown",
    }

    if not isinstance(training_load, Exception) and training_load:
        fitness["training_load"] = training_load
        tsb = training_load.get("tsb")
        if tsb is not None:
            fitness["fatigue_level"] = "fresh" if tsb > 5 else ("tired" if tsb < -15 else "normal")

    if not isinstance(activities_48h, Exception) and activities_48h:
        fitness["load_48h"] = activities_48h
        fitness["total_km"] = activities_48h.get("km_48h", 0)
        fitness["run_count"] = activities_48h.get("sessions_48h", 0)

    if not isinstance(last_race, Exception) and last_race:
        fitness["last_race"] = last_race

    if not isinstance(best_efforts, Exception) and best_efforts:
        fitness["predictions"] = best_efforts

    return fitness


async def get_coros_fitness_data(db_user_id: int) -> dict | None:
    """
    Получает данные атлета из COROS — аналог get_garmin_fitness_data().
    Возвращает dict, совместимый со структурой fitness для промта.
    """
    if get_token(db_user_id, "coros_mcp"):
        try:
            import coros_mcp as _cm
            data = await _cm.get_full_data(db_user_id)
            if data:
                return data
        except Exception as e:
            logger.error(f"COROS (новый) fitness data error for {db_user_id}: {e}")
    import coros as _coros
    if not get_token(db_user_id, "coros"):
        return None
    try:
        return await _coros.get_full_data(db_user_id)
    except Exception as e:
        logger.error(f"COROS fitness data error for {db_user_id}: {e}")
        return None


async def get_polar_fitness_data(db_user_id: int) -> dict | None:
    """
    Получает данные атлета из Polar — аналог get_coros_fitness_data().
    Возвращает dict, совместимый со структурой fitness для промта.
    """
    import polar as _polar
    if not get_token(db_user_id, "polar"):
        return None
    try:
        return await _polar.get_full_data(db_user_id)
    except Exception as e:
        logger.error(f"Polar fitness data error for {db_user_id}: {e}")
        return None


async def _get_vo2max_from_tracker(db_user_id: int) -> tuple:
    """VO2max из ГЛАВНОГО трекера человека (database.primary_vo2max_tracker: первый подключённый
    из Garmin → COROS новый → COROS старый → Polar).
    Возвращает (vo2max: float, tracker_key: str, tracker_name: str) или (None, None, None).
    05.10.2026 (Антон): к следующему трекеру НЕ переходим, если главный ничего не дал — тогда
    в профиле остаётся его прежнее значение. Раньше пустой ответ Garmin перетирался запасным COROS."""
    from database import primary_vo2max_tracker
    primary = primary_vo2max_tracker(db_user_id)
    if not primary:
        return None, None, None
    try:
        if primary == "garmin":
            from garmin import get_vo2max as _get
            name = "Garmin"
        elif primary == "coros_mcp":
            from coros_mcp import get_vo2max as _get
            name = "COROS"
        elif primary == "coros":
            from coros import get_vo2max as _get
            name = "COROS"
        else:
            from polar import get_vo2max as _get
            name = "Polar"
        val = await _get(db_user_id)
        if val is not None:
            return float(val), primary, name
    except Exception as e:
        logger.warning(f"VO2max {primary} fetch error for uid={db_user_id}: {e}")
    return None, None, None
