"""Сборка пакета данных для ИИ-анализа тренировки (read-only).

Канонический модуль: используется и админ-командой бота, и отладочным
scripts/ai_data_package.py. Собирает по DD-активности всё, что нужно ИИ по таблице,
КРОМЕ лактата и субъективных оценок (источника нет) и погоды (пока нет источника).

Источники:
  PROFILE   database.get_user_profile   — пол, возраст, МПК, ПАНО
  S4        workout_analysis по дате     — суть/цель/интенсивность (анализ анонса)
  PLAN      activity_review._flatten_plan_steps — структура и целевые темпы (Garmin workout)
  SPLITS    lapDTO                        — факт по отрезкам (время/темп/ЧСС/каденс/мощность/
                                            GCT/верт.колеб/баланс/дыхание/compliance)
  DETAILS   get_activity_details (1 Гц)   — ЧСС перед стартом повтора; сплиты по 200 м
  MORNING   database.get_morning_caught   — текущий утренний снимок (TR/BB/HRV/RHR/сон)

Привязка точек DETAILS к лэпу — по времени (directTimestamp vs startTimeGMT как UTC).
Валидация сплитов: пройденная дистанция ≈ lapDTO.distance, иначе не считаем (без подстановок).

Главная точка: build_package(db_user_id, selector=None) -> {ok, msg, name, text}.
text — готовый текстовый пакет (без промпта). PROMPT — инструкция для ИИ (добавляется
вызывающим, если нужно). НЕ импортирует bot.py.
"""
import os
import re
import bisect
import asyncio
from datetime import datetime, timezone, date

import garmin
import database as db
import activity_review as ar

_fmt_pace = ar._pace_formatter
_fmt_time = ar._fmt_time

PROMPT = (
    "Вот полные данные моей тренировки: возраст, МПК, ПАНО, целевой план Garmin, "
    "таблица с темпом, пульсом, биомеханикой по каждому отрезку, и самочувствие наутро "
    "(Training Readiness, сон, HRV).\n"
    "Задача: проанализируй тренировку как тренер бегового клуба. Требования к ответу:\n\n"
    "* только суть, живым языком, без лишних цифр\n"
    "* сначала оцени, соблюдён ли план по темпу и по отдыху (особенно если есть расхождения)\n"
    "* на ПОСЛЕДНЕМ рабочем отрезке допустимо отклонение от задания; если он быстрее цели — "
    "игнорируй это отклонение и не считай его ошибкой\n"
    "* определи, не была ли тренировка слишком тяжёлой, и если да – то что именно перегружено: "
    "темп, количество повторов, восстановление\n"
    "* дай конкретную рекомендацию: что изменить в следующий раз (меньше повторов, другой темп, другая пауза)\n"
    "* не пиши общие фразы про биомеханику, если только там нет явных проблем\n\n"
    "ФОРМАТ ОТВЕТА (это сообщение в Telegram, без Markdown):\n"
    "* НЕ используй звёздочки **, решётки # и любую Markdown-разметку\n"
    "* раздели ответ на короткие смысловые блоки, между блоками — ПУСТАЯ СТРОКА\n"
    "* каждый блок начинай со строки-заголовка с эмодзи, например:\n"
    "  «📋 План и отдых», «🔥 Нагрузка», «✅ Рекомендация на следующий раз»\n"
    "* внутри блока 2–4 коротких предложения или маркеры «— » с новой строки\n"
    "* не лепи всё в один абзац\n\n"
    "Данные:"
)


def _age(birthdate):
    try:
        b = datetime.strptime(str(birthdate)[:10], "%Y-%m-%d").date()
        t = date.today()
        return t.year - b.year - ((t.month, t.day) < (b.month, b.day))
    except Exception:
        return None


def _gmt_ms(s):
    """startTimeGMT 'YYYY-MM-DDTHH:MM:SS(.s)' как UTC → epoch ms. None если не парсится."""
    if not s:
        return None
    try:
        base = str(s).split(".")[0]
        dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _num(v, nd=0):
    if v is None:
        return "—"
    return f"{v:.{nd}f}" if nd else f"{int(round(v))}"


def _parse_details(details):
    """Отсортированные по времени точки (t_ms, dist_m, speed_ms, hr) или None."""
    if not isinstance(details, dict):
        return None
    idx = {}
    for d in (details.get("metricDescriptors") or []):
        idx[d.get("key")] = d.get("metricsIndex")
    rows = details.get("activityDetailMetrics") or []
    it, idd, isp, ihr = (idx.get("directTimestamp"), idx.get("sumDistance"),
                         idx.get("directSpeed"), idx.get("directHeartRate"))
    if it is None:
        return None
    pts = []
    for r in rows:
        m = r.get("metrics") if isinstance(r, dict) else None
        if not m:
            continue

        def g(i):
            return m[i] if (i is not None and i < len(m)) else None

        t = g(it)
        if t is None:
            continue
        pts.append((t, g(idd), g(isp), g(ihr)))
    pts.sort(key=lambda x: x[0])
    return pts or None


def _hr_before(pts, ts):
    """ЧСС точки непосредственно перед отсечкой ts (epoch ms). None если нет."""
    if not pts or ts is None:
        return None
    times = [p[0] for p in pts]
    i = bisect.bisect_left(times, ts) - 1
    if i < 0:
        return None
    return pts[i][3]


def _splits_200(pts, start_ms, end_ms, lap_dist):
    """Сплиты по 200 м внутри отрезка (для длинных). [темп_сек] или None.
    Последний неполный кусок — по фактической дистанции (без подстановок).
    Валидация: дистанция по точкам ≈ lap_dist (±10%)."""
    seg = [p for p in pts if p[0] is not None and start_ms <= p[0] < end_ms and p[1] is not None]
    if len(seg) < 4 or not lap_dist or lap_dist < 400:
        return None
    d0 = seg[0][1]
    covered = seg[-1][1] - d0
    if abs(covered - lap_dist) > max(20, 0.10 * lap_dist):
        return None
    out = []
    chunk = 200.0
    target = d0 + chunk
    t_start = seg[0][0]
    for t, dist, _, _ in seg:
        if dist >= target:
            dt = (t - t_start) / 1000.0
            if dt > 0:
                out.append(round(dt / (chunk / 1000.0), 1))
            t_start = t
            target += chunk
    last_t, last_d = seg[-1][0], seg[-1][1]
    rem_d = last_d - (target - chunk)
    rem_t = (last_t - t_start) / 1000.0
    if rem_d > 0 and rem_t > 0:
        out.append(round(rem_t / (rem_d / 1000.0), 1))
    return out or None


def _plan_text(plan_steps):
    if not plan_steps:
        return "  нет (workout не привязан)"
    lines = []
    for s in plan_steps:
        dist = f"{int(s['dist'])}м" if s.get("dist") else "?"
        if s["bounds"]:
            slow, fast = s["bounds"]
            tgt = (f"{_fmt_pace(slow)}" if abs(slow - fast) <= ar.WORK_EXACT_EPS
                   else f"{_fmt_pace(slow)}→{_fmt_pace(fast)}")
        else:
            tgt = "без цели"
        role = {"interval": "работа", "recovery": "отдых"}.get(s["stype"], s["stype"])
        lines.append(f"  шаг {s['idx']}: {role} {dist} — цель {tgt}")
    return "\n".join(lines)


def _s4_by_date(workout_date, workout_type):
    """analyzed_json последнего валидного анализа за дату (точное совпадение)."""
    import json
    if not workout_date:
        return None
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT analyzed_json FROM workout_analysis "
            "WHERE workout_date = ? AND is_valid = 1 "
            "ORDER BY (workout_type = ?) DESC, updated_at DESC LIMIT 1",
            (workout_date, workout_type or "")
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def _enrich_laps(splits, plan_steps, pts):
    """Лэпы в хронологии с полным набором полей + индекс i.j + ЧСС перед стартом (work)."""
    laps = (splits.get("lapDTOs") or []) if isinstance(splits, dict) else []
    laps = [l for l in laps if isinstance(l, dict)]
    starts = [_gmt_ms(l.get("startTimeGMT")) for l in laps]

    def role(lp):
        return ar._role_of(lp.get("wktStepIndex"), str(lp.get("intensityType") or "").upper(), plan_steps)

    work_steps = sorted({lp.get("wktStepIndex") for lp in laps
                         if role(lp) == "work" and lp.get("wktStepIndex") is not None})
    j_of = {st: k + 1 for k, st in enumerate(work_steps)}
    S = len(work_steps)

    rows, occ = [], {}
    for n, lp in enumerate(laps):
        st = lp.get("wktStepIndex")
        if st is None:          # хвост-добегание (нет шага плана)
            continue
        d = lp.get("distance")
        t = lp.get("duration") or lp.get("movingDuration")
        if not d or not t:
            continue
        rl = role(lp)
        label = ""
        if rl == "work" and st in j_of:
            occ[st] = occ.get(st, 0) + 1
            label = f"{occ[st]}" if S == 1 else f"{occ[st]}.{j_of[st]}"
        start_ms = starts[n]
        end_ms = starts[n + 1] if n + 1 < len(starts) else (start_ms + int(t * 1000) if start_ms else None)
        hr_before = _hr_before(pts, start_ms) if (rl == "work" and pts) else None
        sp200 = _splits_200(pts, start_ms, end_ms, d) if (rl == "work" and pts and end_ms) else None
        rows.append({
            "label": label, "role": rl, "dist": d, "dur": t,
            "pace": t / (d / 1000),
            "avg_hr": lp.get("averageHR"), "max_hr": lp.get("maxHR"),
            "cad": lp.get("averageRunCadence"),
            "pwr": lp.get("averagePower"), "npwr": lp.get("normalizedPower"), "mpwr": lp.get("maxPower"),
            "gct": lp.get("groundContactTime"), "vo": lp.get("verticalOscillation"),
            "bal": lp.get("groundContactBalanceLeft"), "resp": lp.get("avgRespirationRate"),
            "compl": lp.get("directWorkoutComplianceScore"),
            "hr_before": hr_before, "splits200": sp200,
        })
    return rows, S


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _pick_activity(acts, selector):
    runs = [a for a in (acts or [])
            if "running" in str((a.get("activityType") or {}).get("typeKey", ""))
            and re.search(r"DD[-_]", str(a.get("activityName") or ""))]
    if selector is None:
        return runs[0] if runs else None
    if str(selector).isdigit():
        return next((a for a in (acts or []) if str(a.get("activityId")) == str(selector)), None)
    return next((a for a in runs if str(selector) in str(a.get("activityName") or "")), None)


def _date_from_name(name):
    """(wdate 'YYYY-MM-DD', wgroup) из имени по маске DD<разд>YYYYMMDD<разд><группа><разд>lvl.
    Разделители '-' и '_' считаются эквивалентными."""
    m = re.search(r"DD[-_](\d{8})", name or "")
    wdate = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}" if m else None
    mg = re.search(r"DD[-_]\d{8}[-_]([\d.]+)[-_]lvl", name or "")
    return wdate, (mg.group(1) if mg else None)


def _template_json(wdate, wgroup):
    """Распарсенный JSON эталона из workout_templates или None."""
    if not (wdate and wgroup):
        return None
    import json as _json
    tmpl = db.get_workout_template(wdate, wgroup, "interval")
    if not tmpl:
        return None
    try:
        return _json.loads(tmpl)
    except Exception:
        return None


async def _garmin_candidate(db_user_id, selector):
    """Кандидат из Garmin (последняя DD-активность) или None.
    План: Garmin workout по workoutId, иначе фолбэк на workout_templates."""
    client = await garmin._client(db_user_id)
    if not client:
        return None
    acts = await asyncio.to_thread(client.get_activities, 0, 20)
    act = _pick_activity(acts, selector)
    if not act:
        return None
    name = act.get("activityName")
    wdate, wgroup = _date_from_name(name)
    act_id = act.get("activityId")
    wkt_id = act.get("workoutId")
    splits = await asyncio.to_thread(client.get_activity_splits, act_id)
    plan_steps = []
    if wkt_id:
        try:
            wkt = await asyncio.to_thread(client.get_workout_by_id, wkt_id)
            plan_steps = ar._flatten_plan_steps(wkt)
        except Exception:
            plan_steps = []
    if not plan_steps:
        wkt = _template_json(wdate, wgroup)
        if wkt:
            plan_steps = ar._flatten_plan_steps(wkt)
    try:
        details = await asyncio.to_thread(client.get_activity_details, act_id, 100000, 100000)
    except Exception:
        details = None
    return {"source": "garmin", "name": name, "act_id": act_id,
            "display_date": act.get("startTimeLocal"), "wdate": wdate, "wgroup": wgroup,
            "wtype_key": (act.get("activityType") or {}).get("typeKey"),
            "splits": splits, "plan_steps": plan_steps, "pts": _parse_details(details)}


async def _strava_candidate(db_user_id, selector):
    """Кандидат из Strava (последняя DD-активность) или None.
    План берётся из workout_templates (без него размечать лэпы нечем → None).
    pts нет (Strava не отдаёт 1 Гц через этот путь) — ЧСС-перед/сплиты-200 будут пусты."""
    import strava
    token = await strava.ensure_valid_token(db_user_id)
    if not token:
        return None
    acts = await strava.get_recent_activities(token, days=30)
    runs = [a for a in (acts or [])
            if a.get("type") == "Run" and re.search(r"DD[-_]", str(a.get("name") or ""))]
    if selector is None:
        act = runs[0] if runs else None
    elif str(selector).isdigit():
        act = next((a for a in (acts or []) if str(a.get("id")) == str(selector)), None)
    else:
        act = next((a for a in runs if str(selector) in str(a.get("name") or "")), None)
    if not act:
        return None
    name = act.get("name")
    wdate, wgroup = _date_from_name(name)
    plan_wkt = _template_json(wdate, wgroup)
    if not plan_wkt:
        return None
    plan_steps = ar._flatten_plan_steps(plan_wkt)
    splits = await strava.get_activity_splits(token, act.get("id"), plan_wkt)
    return {"source": "strava", "name": name, "act_id": act.get("id"),
            "display_date": act.get("start_date_local"), "wdate": wdate, "wgroup": wgroup,
            "wtype_key": "running", "splits": splits, "plan_steps": plan_steps, "pts": None}


def _choose_candidate(g, s):
    """Более новый по дате-из-имени; при равенстве — Garmin."""
    if g and not s:
        return g
    if s and not g:
        return s
    if not g and not s:
        return None
    return s if (s["wdate"] or "") > (g["wdate"] or "") else g


async def build_package(db_user_id: int, selector=None) -> dict:
    """Собирает пакет данных для ИИ по DD-активности.
    selector: None → последняя DD; маска 'DD_YYYYMMDD'; либо activityId.
    Возвращает {ok, msg, name, text}. text — пакет без промпта (PROMPT добавляет вызывающий)."""
    g = await _garmin_candidate(db_user_id, selector)
    s = await _strava_candidate(db_user_id, selector)
    cand = _choose_candidate(g, s)
    if not cand:
        sel = f" по «{selector}»" if selector else ""
        return {"ok": False, "msg": f"DD-активность{sel} не найдена (Garmin/Strava)."}

    name = cand["name"]
    act_id = cand["act_id"]
    wdate = cand["wdate"]
    splits = cand["splits"]
    plan_steps = cand["plan_steps"]
    pts = cand["pts"]

    prof = db.get_user_profile(db_user_id) or {}
    snap = db.get_morning_caught(db_user_id)
    s4 = _s4_by_date(wdate, cand["wtype_key"])
    rows, S = _enrich_laps(splits, plan_steps, pts)

    L = []
    A = L.append
    A("=" * 64)
    A("ПАКЕТ ДАННЫХ ДЛЯ АНАЛИЗА ТРЕНИРОВКИ")
    A("=" * 64)
    A(f"Тренировка: {name}")
    A(f"Дата: {cand['display_date']}   activityId: {act_id}   источник: {cand['source']}")

    A("\n[СПОРТСМЕН]")
    A(f"  Пол: {prof.get('gender') or '—'}   Возраст: {_age(prof.get('birthdate')) or '—'}")
    A(f"  МПК: {prof.get('vo2max') or '—'}   "
      f"ПАНО: {prof.get('lactate_threshold_pace') or '—'}/км @ {prof.get('lactate_threshold_hr') or '—'} уд/мин")
    A(f"  Специализация: {prof.get('specialization') or '—'}")

    A("\n[ЦЕЛЬ И СУТЬ ТРЕНИРОВКИ] (из анализа анонса)")
    if s4:
        A(f"  Тип: {s4.get('workout_type')}   Интенсивность: {s4.get('intensity_level')}")
        if s4.get("summary"):
            A(f"  Суть: {s4['summary']}")
        if s4.get("overall_purpose"):
            A(f"  Цель: {s4['overall_purpose']}")
        if s4.get("what_to_watch"):
            A(f"  На что смотреть: {s4['what_to_watch']}")
    else:
        A("  нет анализа за эту дату")

    A("\n[ПЛАН] (эталон)")
    A(_plan_text(plan_steps))

    work = [r for r in rows if r["role"] == "work"]
    rest = [r for r in rows if r["role"] == "rest"]

    A("\n[ФАКТ — ТЕМП И ПУЛЬС ПО ОТРЕЗКАМ]")
    A(f"  {'отр':>5} {'роль':<6} {'дист':>5} {'время':>6} {'темп':>6} "
      f"{'ЧССср':>5} {'ЧССмакс':>7} {'ЧССперед':>8}")
    for r in rows:
        A(f"  {r['label'] or '·':>5} {r['role']:<6} {_num(r['dist']):>4}м "
          f"{_fmt_time(r['dur']):>6} {_fmt_pace(r['pace']):>6} "
          f"{_num(r['avg_hr']):>5} {_num(r['max_hr']):>7} {_num(r['hr_before']):>8}")
    if work:
        A(f"  средн. работа: темп {_fmt_pace(_avg([r['pace'] for r in work]))}  "
          f"ЧССср {_num(_avg([r['avg_hr'] for r in work]))}")
    if rest:
        A(f"  средн. отдых:  темп {_fmt_pace(_avg([r['pace'] for r in rest]))}  "
          f"ЧССср {_num(_avg([r['avg_hr'] for r in rest]))}")

    A("\n[ФАКТ — БИОМЕХАНИКА И МОЩНОСТЬ ПО ОТРЕЗКАМ]")
    A(f"  {'отр':>5} {'роль':<6} {'кад':>4} {'мощн':>5} {'NP':>4} {'GCTмс':>5} "
      f"{'ВКсм':>5} {'балL':>5} {'дых':>4} {'compl':>5}")
    for r in rows:
        A(f"  {r['label'] or '·':>5} {r['role']:<6} {_num(r['cad']):>4} "
          f"{_num(r['pwr']):>5} {_num(r['npwr']):>4} {_num(r['gct']):>5} "
          f"{_num(r['vo'], 1):>5} {_num(r['bal'], 1):>5} {_num(r['resp']):>4} {_num(r['compl']):>5}")

    sp_rows = [r for r in work if r.get("splits200")]
    if sp_rows:
        A("\n[СПЛИТЫ ПО 200 м ВНУТРИ ДЛИННЫХ ОТРЕЗКОВ] (темп каждого 200 м)")
        for r in sp_rows:
            A(f"  отр {r['label']}: " + ", ".join(_fmt_pace(p) for p in r["splits200"]))

    A("\n[САМОЧУВСТВИЕ УТРОМ] (текущий снимок)")
    if snap and snap.get("caught"):
        A(f"  снимок за {snap.get('date')}")
        A(f"  Training Readiness: {_num(snap.get('tr'))}   Body Battery: {_num(snap.get('bb'))}")
        A(f"  Сон: {_num(snap.get('sleep_h'), 1)}ч   HRV: {_num(snap.get('hrv'))}   "
          f"ЧСС покоя: {_num(snap.get('rhr'))}   Пробуждение: {snap.get('wake_at') or '—'}")
    else:
        A("  снимка нет")

    A("\n[НЕТ ДАННЫХ] лактат, субъективная оценка (RPE), погода")
    A("=" * 64)

    return {"ok": True, "name": name, "text": "\n".join(L), "msg": "",
            "splits": splits, "plan_steps": plan_steps}


async def build_charts(splits, plan_steps, name: str, out_dir: str,
                       tag: str, dark: bool = True) -> dict:
    """Строит 3 PNG (work/rest/table) из уже добытых splits+plan_steps.
    Переиспользует рисовалки activity_review (без повторного похода в Garmin).
    tag — уникальный суффикс имён файлов (напр. db_user_id_activityId).
    Возвращает {work_png, rest_png, table_png} (любой может быть None)."""
    ar.DARK_MODE = dark
    ordered = ar._ordered_laps(splits)
    work_roles, x_ticks, rest_paces, S = ar._segment_model(ordered, plan_steps)

    base = os.path.join(out_dir, f"ai_{tag}")
    work_png = await asyncio.to_thread(
        ar._plot_work_segmented, work_roles, x_ticks,
        f"Тренировка: {name}", base + "_work.png")

    rest_plan = next((s for s in plan_steps if s["stype"] == "recovery" and s["bounds"]), None)
    rest_target = sum(rest_plan["bounds"]) / 2.0 if rest_plan else None
    rest_png = await asyncio.to_thread(
        ar._plot_rest, rest_paces, rest_target,
        "Анализ восстановительных интервалов", base + "_rest.png") if rest_paces else None

    ws, rmeta, rw, rr, maxi = ar._table_model(ordered, plan_steps)
    table_png = await asyncio.to_thread(
        ar._plot_table_segmented, ws, rmeta, rw, rr, maxi, bool(rest_paces), rest_target,
        "Повторы: время / темп / отклонение", base + "_table.png") if (ws and maxi) else None

    return {"work_png": work_png, "rest_png": rest_png, "table_png": table_png}


async def build_report_card(splits, plan_steps, name: str, wdate, wgroup, source: str,
                            s4: dict | None, out_dir: str, tag: str,
                            dark: bool = False) -> str | None:
    """Вертикальная карточка разбора под телефон (портрет, три зоны сверху вниз):
    1) шапка — заголовок, название/дата/группа, суть, структура плана;
    2) факт — таблица повторов (зебра, заливка отклонений, строка «ср.»);
    3) итоги — плашки (ср. темп работы vs цель, ср. отдых, повторы).
    ПАРАЛЛЕЛЬНАЯ боевой _plot_table_segmented — activity_review не меняет.
    Возвращает путь к PNG или None."""
    import textwrap
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch, Rectangle

    ar.DARK_MODE = dark
    ordered = ar._ordered_laps(splits)
    ws, rmeta, rw, rr, maxi = ar._table_model(ordered, plan_steps)
    if not ws or not maxi:
        return None
    rest_laps = [l for l in ordered
                 if ar._role_of(l["step"], l["intensity"], plan_steps) == "rest"]
    work_laps = [l for l in ordered
                 if ar._role_of(l["step"], l["intensity"], plan_steps) == "work"]
    rest_plan = next((s for s in plan_steps if s["stype"] == "recovery" and s["bounds"]), None)
    rest_target = sum(rest_plan["bounds"]) / 2.0 if rest_plan else None
    has_rest = bool(rest_laps)

    def _dev(fact, et):
        if et is None or fact is None:
            return "—", None
        d = int(round(fact - et))
        if d == 0:
            return "0", None
        sign = "+" if d > 0 else "−"
        return f"{sign}{abs(d)}", ar._delta_color(abs(d))

    # ── Данные таблицы (зона 2) ──
    headers = ["№"]
    for st in ws:
        lbl = rmeta[st]["label"]
        headers += [f"{lbl}\nвремя", f"{lbl}\nтемп", f"{lbl}\nоткл"]
    if has_rest:
        headers += ["Отдых\nвремя", "Отдых\nтемп", "Отдых\nоткл"]

    rows, cell_fill = [], {}
    _FILL = {"green": "#2e7d32", "gold": "#b8860b", "red": "#c62828"}
    for i in range(1, maxi + 1):
        row = [str(i)]
        col = 1
        for st in ws:
            cell = rw.get(i, {}).get(st)
            if cell:
                dur, pace = cell
                dev, color = _dev(pace, ar._seg_etalon(rmeta[st], i))
                row += [_fmt_time(dur), _fmt_pace(pace), dev]
                if color:
                    cell_fill[(i, col + 2)] = _FILL[color]
            else:
                row += ["—", "—", "—"]
            col += 3
        if has_rest:
            rc = rr.get(i)
            if rc:
                dev, color = _dev(rc[1], rest_target)
                row += [_fmt_time(rc[0]), _fmt_pace(rc[1]), dev]
                if color:
                    cell_fill[(i, col + 2)] = _FILL[color]
            else:
                row += ["—", "—", "—"]
        rows.append(row)

    avg_row = ["ср."]
    for st in ws:
        cells = [rw[i][st] for i in rw if st in rw[i]]
        durs = [c[0] for c in cells if c[0] is not None]
        pcs = [c[1] for c in cells if c[1] is not None]
        avg_row += [_fmt_time(sum(durs) / len(durs)) if durs else "—",
                    _fmt_pace(sum(pcs) / len(pcs)) if pcs else "—", ""]
    if has_rest:
        durs = [v[0] for v in rr.values() if v[0] is not None]
        pcs = [v[1] for v in rr.values() if v[1] is not None]
        avg_row += [_fmt_time(sum(durs) / len(durs)) if durs else "—",
                    _fmt_pace(sum(pcs) / len(pcs)) if pcs else "—", ""]
    rows.append(avg_row)
    avg_r = maxi + 1

    # ── Шапка (зона 1) ──
    meta_bits = [b for b in (wdate, f"группа {wgroup}" if wgroup else None, source) if b]
    meta_line = "  ·  ".join(meta_bits)
    summary = ""
    if s4:
        summary = (s4.get("summary") or s4.get("overall_purpose") or "").strip()
    sum_lines = textwrap.wrap(summary, width=62)[:3] if summary else []
    if summary and len(textwrap.wrap(summary, width=62)) > 3:
        sum_lines[-1] = sum_lines[-1].rstrip(".,;… ") + "…"
    # Структура плана: «7 × 600 м @ 3:52→3:59  ·  отдых 200 м @ 6:10»
    plan_bits = []
    for st in ws:
        m = rmeta[st]
        b = m["bounds"]
        if b:
            slow, fast = b
            tgt = (_fmt_pace((slow + fast) / 2) if abs(slow - fast) <= ar.WORK_EXACT_EPS
                   else f"{_fmt_pace(slow)}→{_fmt_pace(fast)}")
            plan_bits.append(f"{m['n']} × {m['label']} @ {tgt}")
        else:
            plan_bits.append(f"{m['n']} × {m['label']}")
    if rest_plan:
        rd = f"{int(rest_plan['dist'])} м " if rest_plan.get("dist") else ""
        plan_bits.append(f"отдых {rd}@ {_fmt_pace(rest_target)}")
    plan_line = "  ·  ".join(plan_bits)

    # ── Итоги (зона 3) ──
    work_avg = _avg([l["pace"] for l in work_laps])
    rest_avg = _avg([l["pace"] for l in rest_laps]) if has_rest else None
    b0 = rmeta[ws[0]]["bounds"]
    if b0:
        s0, f0 = b0
        work_goal = (_fmt_pace((s0 + f0) / 2) if abs(s0 - f0) <= ar.WORK_EXACT_EPS
                     else f"{_fmt_pace(s0)}→{_fmt_pace(f0)}")
    else:
        work_goal = "—"
    plaques = [("СР. ТЕМП РАБОТЫ", _fmt_pace(work_avg) if work_avg else "—",
                f"цель: {work_goal}", "#1f77b4")]
    if has_rest:
        plaques.append(("СР. ТЕМП ОТДЫХА", _fmt_pace(rest_avg) if rest_avg else "—",
                        f"цель: {_fmt_pace(rest_target) if rest_target else '—'}", "#ff8c00"))
    plaques.append(("ПОВТОРЫ", str(len(work_laps)),
                    "выполнено", "#2e7d32"))

    # ── Компоновка: портрет, три зоны ──
    th = ar._theme()
    accent = "#ff8c00"
    n_hdr_lines = 3 + len(sum_lines) + (1 if plan_line else 0)
    hdr_in = 0.42 * n_hdr_lines + 0.5
    tbl_in = 0.46 * (maxi + 2)
    tot_in = 1.9
    fig_h = hdr_in + tbl_in + tot_in + 0.6
    fig_w = 9.0
    with plt.rc_context(ar._rc()):
        fig = plt.figure(figsize=(fig_w, fig_h))
        gs = fig.add_gridspec(3, 1, height_ratios=[hdr_in, tbl_in, tot_in],
                              hspace=0.04, left=0.03, right=0.97, top=0.985, bottom=0.02)
        ax_h = fig.add_subplot(gs[0]); ax_h.axis("off")
        ax_t = fig.add_subplot(gs[1]); ax_t.axis("off")
        ax_b = fig.add_subplot(gs[2]); ax_b.axis("off")

        # Зона 1: шапка
        y = 1.0
        dy = 1.0 / max(n_hdr_lines + 1, 4)
        ax_h.text(0, y, "РАЗБОР ТРЕНИРОВКИ", fontsize=20, fontweight="bold",
                  color=accent, va="top", ha="left", transform=ax_h.transAxes)
        y -= dy * 1.25
        ax_h.text(0, y, name or "", fontsize=13, fontweight="bold",
                  va="top", ha="left", transform=ax_h.transAxes)
        y -= dy
        ax_h.text(0, y, meta_line, fontsize=10.5, alpha=0.8,
                  va="top", ha="left", transform=ax_h.transAxes)
        y -= dy
        for ln in sum_lines:
            ax_h.text(0, y, ln, fontsize=10.5, style="italic",
                      va="top", ha="left", transform=ax_h.transAxes)
            y -= dy
        if plan_line:
            ax_h.text(0, y, "ПЛАН: " + plan_line, fontsize=11, fontweight="bold",
                      va="top", ha="left", transform=ax_h.transAxes)

        # Зона 2: таблица (bbox на всю зону — без разрывов)
        tbl = ax_t.table(cellText=rows, colLabels=headers,
                         cellLoc="center", bbox=[0, 0, 1, 1])
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(10)
        zebra = "#2a2a2a" if dark else "#f2f2f2"
        hdr_bg = "#333333" if not dark else "#3a3a3a"
        for (r, c), cell in tbl.get_celld().items():
            cell.set_edgecolor("#999999")
            if r == 0:
                cell.set_facecolor(hdr_bg)
                cell.set_text_props(fontweight="bold", color="white")
            elif r == avg_r:
                cell.set_facecolor(th["box_face"])
                cell.set_text_props(fontweight="bold", color=th["text"])
            else:
                cell.set_facecolor(zebra if r % 2 == 0 else "none")
                cell.set_text_props(color=th["text"])
        for (r, c), fill in cell_fill.items():
            tbl[r, c].set_facecolor(fill)
            tbl[r, c].set_text_props(color="white", fontweight="bold")

        # Зона 3: плашки итогов
        ax_b.set_xlim(0, 1); ax_b.set_ylim(0, 1)
        k = len(plaques)
        pw = min(0.30, 0.96 / k - 0.02)
        gap = (1.0 - k * pw) / (k + 1)
        for idx, (title, big, small, color) in enumerate(plaques):
            x0 = gap + idx * (pw + gap)
            ax_b.add_patch(FancyBboxPatch((x0, 0.12), pw, 0.74,
                           boxstyle="round,pad=0.015", linewidth=2,
                           edgecolor=color, facecolor="none",
                           transform=ax_b.transAxes))
            cx = x0 + pw / 2
            ax_b.text(cx, 0.74, title, fontsize=10, fontweight="bold", color=color,
                      ha="center", va="center", transform=ax_b.transAxes)
            ax_b.text(cx, 0.47, big, fontsize=22, fontweight="bold", color=color,
                      ha="center", va="center", transform=ax_b.transAxes)
            ax_b.text(cx, 0.22, small, fontsize=9.5, alpha=0.85,
                      ha="center", va="center", transform=ax_b.transAxes)
        ax_b.text(0.99, 0.0, "DoDick · @DD_adviser_bot", fontsize=8.5, alpha=0.6,
                  ha="right", va="bottom", transform=ax_b.transAxes)

        out_path = os.path.join(out_dir, f"card_{tag}.png")
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
    return out_path


async def analyze_with_ai(db_user_id: int, selector=None, mode: str = "deep") -> dict:
    """Собирает пакет и отправляет его в DeepSeek с промптом-инструкцией.
    Возвращает {ok, msg, name, answer, package}. answer — свободный текст анализа от ИИ."""
    pkg = await build_package(db_user_id, selector)
    if not pkg.get("ok"):
        return pkg
    import claude_advisor
    prompt = PROMPT + "\n\n" + pkg["text"]
    answer = await asyncio.to_thread(claude_advisor.ask_text, prompt, mode)
    if not answer:
        return {"ok": False, "msg": "ИИ не ответил (пустой ответ или таймаут)."}
    return {"ok": True, "name": pkg["name"], "answer": answer, "package": pkg["text"], "msg": ""}
