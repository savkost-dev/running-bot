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
    "* сначала оцени, соблюдён ли план по темпу и по отдыху (особенно если есть расхождения); "
    "если в данных НЕТ отрезков с ролью rest — тренировка непрерывная, про отдых и паузы "
    "не пиши вообще ни слова\n"
    "* на ПОСЛЕДНЕМ рабочем отрезке допустимо отклонение от задания; если он быстрее цели — "
    "игнорируй это отклонение и не считай его ошибкой\n"
    "* определи, не была ли тренировка слишком тяжёлой, и если да – то что именно перегружено: "
    "темп, количество повторов, восстановление\n"
    "* если вся работа стабильно быстрее плана — не считай это ошибкой автоматически: сам оцени по данным, "
    "была ли она чрезмерной (пульс относительно ПАНО, развал темпа к концу, рост времени или темпа "
    "восстановления от повтора к повтору, деградация биомеханики). Если признаков перегруза нет — "
    "похвали за запас и предложи в следующий раз попробовать более быструю группу; если признаки есть — "
    "прямо назови их и чем это грозит\n"
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


def _assign_button_laps(splits, plan_wkt, plan_steps, use_plan_dist):
    """Кнопочные лэпы Garmin (у всех wktStepIndex=None) при наличии плана:
    раздаёт wktStepIndex/intensityType по порядку исполнения (strava._expand_plan_roles),
    лишние лэпы-хвосты остаются без шага.
    use_plan_dist=True (тренажёр/манеж без GPS): дистанция лэпа заменяется плановой
    (датчик врёт) — темп станет факт-время / план-дистанция.
    Мутирует splits. Ничего не делает, если индексы уже есть."""
    laps = (splits.get("lapDTOs") or []) if isinstance(splits, dict) else []
    laps = [l for l in laps if isinstance(l, dict)]
    if not laps or not plan_wkt or not plan_steps:
        return
    if any(l.get("wktStepIndex") is not None for l in laps):
        return
    import strava as _sv
    seq = _sv._expand_plan_roles(plan_wkt)
    dist_of = {p["idx"]: p.get("dist") for p in plan_steps}
    for lap, (idx, stype) in zip(laps, seq):
        lap["wktStepIndex"] = idx
        lap["intensityType"] = "REST" if stype in ("recovery", "rest") else "INTERVAL"
        if use_plan_dist and dist_of.get(idx):
            lap["distance"] = float(dist_of[idx])


async def _garmin_candidate(db_user_id, selector):
    """Кандидат из Garmin (последняя DD-активность) или None.
    План: Garmin workout по workoutId, иначе фолбэк на workout_templates."""
    client = await garmin._client(db_user_id)
    if not client:
        return None
    acts = await asyncio.to_thread(client.get_activities, 0, 60)
    act = _pick_activity(acts, selector)
    if not act:
        return None
    name = act.get("activityName")
    wdate, wgroup = _date_from_name(name)
    act_id = act.get("activityId")
    wkt_id = act.get("workoutId")
    splits = await asyncio.to_thread(client.get_activity_splits, act_id)
    plan_wkt = None
    plan_steps = []
    if wkt_id:
        try:
            plan_wkt = await asyncio.to_thread(client.get_workout_by_id, wkt_id)
            plan_steps = ar._flatten_plan_steps(plan_wkt)
        except Exception:
            plan_wkt, plan_steps = None, []
    if not plan_steps:
        plan_wkt = _template_json(wdate, wgroup)
        if plan_wkt:
            plan_steps = ar._flatten_plan_steps(plan_wkt)
    _assign_button_laps(
        splits, plan_wkt, plan_steps,
        use_plan_dist=("treadmill" in str((act.get("activityType") or {}).get("typeKey") or "")))
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
    def _wpace(rr_):
        d = sum(r["dist"] for r in rr_ if r["dist"])
        t = sum(r["dur"] for r in rr_ if r["dur"])
        return (t / (d / 1000)) if (d and t) else None

    if work:
        A(f"  средн. работа: темп {_fmt_pace(_wpace(work))}  "
          f"ЧССср {_num(_avg([r['avg_hr'] for r in work]))}")
    if rest:
        A(f"  средн. отдых:  темп {_fmt_pace(_wpace(rest))}  "
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
            "splits": splits, "plan_steps": plan_steps,
            "wdate": wdate, "wgroup": cand["wgroup"], "source": cand["source"], "s4": s4}


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


def _series_model(ordered, plan_steps):
    """Хронологическая модель «блоки → серии». Границы блоков — по номеру
    repeat-группы плана (поле grp в plan_steps): серия закрывается при повторе
    шага ВНУТРИ серии ИЛИ при смене группы (переход в новый блок без отдыха
    тоже ловится). Одиночный rest-блок МЕЖДУ блоками (топ-уровневый отдых)
    приклеивается колонкой к последней серии предыдущего блока.
    Возвращает [{steps: [idx...], series: [{idx: lap}, ...]}]."""
    grp_of = {p["idx"]: p.get("grp") for p in plan_steps}
    blocks = []
    cur, cur_steps, cur_grp = {}, [], None

    def flush():
        nonlocal cur, cur_steps
        if not cur:
            return
        last = blocks[-1] if blocks else None
        same = last and last["steps"] == cur_steps
        prefix = (last and len(cur_steps) < len(last["steps"])
                  and last["steps"][:len(cur_steps)] == cur_steps)
        if same or prefix:
            last["series"].append(cur)
        else:
            blocks.append({"steps": list(cur_steps), "series": [cur]})
        cur, cur_steps = {}, []

    for l in ordered:
        st = l["step"]
        g = grp_of.get(st)
        if cur and (st in cur or g != cur_grp):
            flush()
        cur_grp = g
        if st not in cur_steps:
            cur_steps.append(st)
        cur[st] = l
    flush()

    # Одиночный rest-only блок (отдых между блоками) → колонкой в предыдущий.
    merged = []
    for b in blocks:
        rest_only = all(
            ar._role_of(st, next((s[st]["intensity"] for s in b["series"] if st in s), ""),
                        plan_steps) == "rest"
            for st in b["steps"])
        if rest_only and merged and len(b["series"]) == 1:
            prev = merged[-1]
            for st in b["steps"]:
                if st not in prev["steps"]:
                    prev["steps"].append(st)
                prev["series"][-1][st] = b["series"][0][st]
        else:
            merged.append(b)
    return merged


async def build_charts_stacked(splits, plan_steps, name: str, out_dir: str,
                               tag: str, dark: bool = False) -> str | None:
    """Оба графика (работа + отдых) на ОДНОЙ вертикальной картинке под телефон:
    сверху интервалы (сегменты/эталон/тренд/дельты), снизу отдых (коридоры).
    Логика отрисовки повторяет activity_review._plot_work_segmented/_plot_rest,
    но на переданных осях — боевые функции не трогает. Статблоки опущены
    (на телефоне тесно), средние есть в карточке-таблице. Возвращает путь или None."""
    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    ar.DARK_MODE = dark
    ordered = ar._ordered_laps(splits)
    work_roles, x_ticks, rest_paces, S = ar._segment_model(ordered, plan_steps)
    work_roles = [r for r in work_roles if r["ys"]]
    if not work_roles:
        return None
    # Метки тиков — по СЕРИЯМ в хронологии (номер_серии.позиция_work-шага);
    # x-позиции не меняются. Если везде по одному work-шагу — просто номер серии.
    blocks = _series_model(ordered, plan_steps)
    _lbls, s_no = [], 0
    for blk in blocks:
        for ser in blk["series"]:
            s_no += 1
            wpos = 0
            for st in blk["steps"]:
                if st not in ser:
                    continue
                if ar._role_of(st, ser[st]["intensity"], plan_steps) != "work":
                    continue
                wpos += 1
                _lbls.append((s_no, wpos))
    if len(_lbls) == len(x_ticks):
        multi = max(w for _, w in _lbls) > 1
        x_ticks = [(x, f"{s}.{w}" if multi else f"{s}")
                   for (x, _), (s, w) in zip(x_ticks, _lbls)]
    rest_plan = next((s for s in plan_steps if s["stype"] == "recovery" and s["bounds"]), None)
    rest_target = sum(rest_plan["bounds"]) / 2.0 if rest_plan else None
    has_rest = bool(rest_paces)

    th = ar._theme()
    n_rows = 2 if has_rest else 1
    fig_h = 6.2 * n_rows + 1.0
    with plt.rc_context(ar._rc()):
        fig = plt.figure(figsize=(9, fig_h))
        gs = fig.add_gridspec(n_rows, 1, hspace=0.30,
                              left=0.10, right=0.97, top=0.90, bottom=0.06)
        ax = fig.add_subplot(gs[0])
        fig.suptitle(f"Тренировка: {name}", fontsize=13, fontweight="bold")

        # ── верх: рабочие интервалы (как _plot_work_segmented, без статблоков) ──
        for ri, r in enumerate(work_roles):
            xs = np.array(r["xs"], dtype=float)
            ys = np.array(r["ys"], dtype=float)
            c = r["color"]
            tls = ar._SERIES_TREND_LS[ri % len(ar._SERIES_TREND_LS)]
            ax.scatter(xs, ys, color=c, s=60, zorder=3, label=f"{r['label']} — факт")
            if r.get("bounds"):
                slow, fast = r["bounds"]
                if abs(slow - fast) <= ar.WORK_EXACT_EPS:
                    target = (slow + fast) / 2.0
                    ax.axhspan(target - ar.WORK_CORR_YELLOW, target + ar.WORK_CORR_YELLOW,
                               color="gold", alpha=0.15, zorder=1)
                    ax.axhspan(target - ar.WORK_CORR_GREEN, target + ar.WORK_CORR_GREEN,
                               color="green", alpha=0.18, zorder=1)
                    ax.axhline(target, color="red", ls="-", lw=2.6, alpha=0.85, zorder=4,
                               label=f"{r['label']} — эталон {ar._pace_formatter(target)}")
                    et_pts = np.full(len(xs), target)
                else:
                    et_pts = np.linspace(slow, fast, len(xs))
                    ax.plot(xs, et_pts, color="red", ls="-", lw=3.0, alpha=0.85, zorder=4,
                            label=f"{r['label']} — эталон ({ar._pace_formatter(slow)}→{ar._pace_formatter(fast)})")
                ar._draw_deltas(ax, xs, ys, et_pts)
            if len(xs) >= 2:
                a, b = np.polyfit(xs, ys, 1)
                tr = a * xs + b
                ax.plot(xs, tr, color=c, ls=tls, lw=2.0, zorder=4,
                        label=f"{r['label']} — тренд ({ar._pace_formatter(tr[0])}→{ar._pace_formatter(tr[-1])})")
        ax.invert_yaxis()
        ax.set_ylabel("Темп (мин:сек/км)", fontsize=10)
        ax.set_title("Рабочие интервалы", fontsize=11, fontweight="bold")
        ax.yaxis.set_major_formatter(FuncFormatter(ar._pace_formatter))
        if x_ticks:
            ax.set_xticks([x for x, _ in x_ticks])
            ax.set_xticklabels([lbl for _, lbl in x_ticks], fontsize=7)
        ax.grid(True, linestyle=":", alpha=0.6)
        ax.legend(loc="best", fontsize=7.5, framealpha=0.9)

        # ── низ: отдых (как _plot_rest) ──
        if has_rest:
            ax2 = fig.add_subplot(gs[1])
            x2 = np.arange(1, len(rest_paces) + 1)
            y2 = np.array(rest_paces)
            if rest_target:
                g_lo = rest_target - ar.REST_CORRIDOR_SEC
                g_hi = rest_target + ar.REST_CORRIDOR_SEC
                y_lo = rest_target - ar.REST_CORRIDOR_YELLOW
                y_hi = rest_target + ar.REST_CORRIDOR_YELLOW
                ax2.fill_between(x2, y_lo, y_hi, color="gold", alpha=0.2, zorder=1,
                                 label=f"Жёлтая ±{ar.REST_CORRIDOR_YELLOW}с")
                ax2.fill_between(x2, g_lo, g_hi, color="green", alpha=0.2, zorder=1,
                                 label=f"Зелёная ±{ar.REST_CORRIDOR_SEC}с")
                colors = ["green" if g_lo <= v <= g_hi else
                          ("gold" if y_lo <= v <= y_hi else "red") for v in y2]
                ax2.scatter(x2, y2, color=colors, s=60, zorder=3, label="Отдых")
            else:
                ax2.scatter(x2, y2, color=th["fact"], s=60, zorder=3, label="Отдых")
            if len(x2) >= 2:
                from scipy import stats as _st
                slope, intercept, *_ = _st.linregress(x2, y2)
                tr2 = slope * x2 + intercept
                ax2.plot(x2, tr2, "b--", linewidth=2.0, zorder=4,
                         label=f"Тренд ({ar._pace_formatter(tr2[0])}→{ar._pace_formatter(tr2[-1])})")
            ax2.invert_yaxis()
            ax2.set_xlabel("Номер интервала", fontsize=10)
            ax2.set_ylabel("Темп (мин:сек/км)", fontsize=10)
            ax2.set_title("Восстановительные интервалы", fontsize=11, fontweight="bold")
            ax2.yaxis.set_major_formatter(FuncFormatter(ar._pace_formatter))
            ax2.set_xticks(x2)
            ax2.tick_params(axis="x", labelsize=7)
            ax2.grid(True, linestyle=":", alpha=0.6)
            ax2.legend(loc="best", fontsize=7.5, framealpha=0.9)

        fig.text(0.985, 0.005, "DoDick · @DD_adviser_bot", fontsize=8, alpha=0.6,
                 ha="right", va="bottom")
        out_path = os.path.join(out_dir, f"charts_{tag}.png")
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
    return out_path


def _plan_diagram(ax, blocks, plan_steps):
    """Схема структуры работы: блоки → прямоугольники шагов в хронологии.
    Ширина ∝ дистанции, высота ∝ интенсивности (быстрее — выше), отдых — низкий серый.
    Быстрый шаг внутри блока (ускорение) — оранжевый. Под блоком — «×N».
    Данные — blocks из _series_model + plan_steps. Ничего не возвращает."""
    from matplotlib.patches import Rectangle
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    def _plan(st):
        return next((q for q in plan_steps if q["idx"] == st), None)

    def _mid(st):
        p = _plan(st)
        return (sum(p["bounds"]) / 2.0) if (p and p["bounds"]) else None

    def _dist(st, blk):
        p = _plan(st)
        if p and p.get("dist"):
            return float(p["dist"])
        lap = next((s[st] for s in blk["series"] if st in s), None)
        return float(lap["dist"]) if (lap and lap.get("dist")) else 100.0

    def _role(st, blk):
        lap = next((s[st] for s in blk["series"] if st in s), None)
        return ar._role_of(st, lap["intensity"] if lap else "", plan_steps)

    mids = [m for blk in blocks for st in blk["steps"]
            if _role(st, blk) == "work" and (m := _mid(st)) is not None]
    lo, hi = (min(mids), max(mids)) if mids else (None, None)
    total = sum(_dist(st, blk) for blk in blocks for st in blk["steps"])
    if not total:
        return
    GAP_STEP, GAP_BLK = 0.006, 0.035
    n_sgaps = sum(max(len(b["steps"]) - 1, 0) for b in blocks)
    usable = 1.0 - GAP_BLK * max(len(blocks) - 1, 0) - GAP_STEP * n_sgaps
    y0 = 0.34
    x = 0.0
    for bi, blk in enumerate(blocks):
        wmids = [m for st in blk["steps"]
                 if _role(st, blk) == "work" and (m := _mid(st)) is not None]
        slowest = max(wmids) if wmids else None
        bx0 = x
        for si, st in enumerate(blk["steps"]):
            w = usable * _dist(st, blk) / total
            rl = _role(st, blk)
            m = _mid(st)
            if rl == "rest":
                h, fc = 0.18, "#9e9e9e"
            else:
                k = 0.5 if (hi is None or hi == lo or m is None) else (hi - m) / (hi - lo)
                h = 0.22 + 0.22 * k
                fast = (slowest is not None and m is not None and m < slowest - 1.0)
                fc = "#ff8c00" if fast else "#1f77b4"
            ax.add_patch(Rectangle((x, y0), w, h, facecolor=fc, edgecolor="none"))
            d = _dist(st, blk)
            dlab = f"{int(d)} м"
            if w >= 0.055:
                ax.text(x + w / 2, y0 + h / 2, dlab, fontsize=9, fontweight="bold",
                        color="white", ha="center", va="center")
            else:
                ax.text(x + w / 2, y0 + h / 2, dlab, fontsize=7.5, fontweight="bold",
                        color="white", ha="center", va="center", rotation=90)
            if rl == "work" and m is not None:
                ax.text(x + w / 2, y0 - 0.05, _fmt_pace(m), fontsize=8.5, alpha=0.85,
                        ha="center", va="top")
            x += w
            if si < len(blk["steps"]) - 1:
                x += GAP_STEP
        n = len(blk["series"])
        ax.plot([bx0, x], [0.84, 0.84], color="#666666", lw=1.2)
        ax.plot([bx0, bx0], [0.84, 0.74], color="#666666", lw=1.2)
        ax.plot([x, x], [0.84, 0.74], color="#666666", lw=1.2)
        ax.text((bx0 + x) / 2, 1.0, f"× {n}", fontsize=11, fontweight="bold",
                ha="center", va="top")
        if bi < len(blocks) - 1:
            x += GAP_BLK
    ax.add_patch(Rectangle((0, 0.10), 1.0, 0.07, facecolor="#e0e0e0", edgecolor="none"))


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

    # ── Данные таблицы (зона 2): секции по блокам, строки = серии в хронологии ──
    blocks = _series_model(ordered, plan_steps)
    _FILL = {"green": "#2e7d32", "gold": "#b8860b", "red": "#c62828"}
    sections = []
    for blk in blocks:
        n_series = len(blk["series"])
        metas = {}
        for st in blk["steps"]:
            plan = next((p for p in plan_steps if p["idx"] == st), None)
            lap0 = next((s[st] for s in blk["series"] if st in s), None)
            role = ar._role_of(st, lap0["intensity"] if lap0 else "", plan_steps)
            label = ("Отдых" if role == "rest"
                     else ar._step_label(plan_steps, st, lap0["dist"] if lap0 else None))
            metas[st] = {"role": role, "label": label,
                         "bounds": plan["bounds"] if plan else None, "n": n_series}
        sec_headers = ["№"]
        for st in blk["steps"]:
            lbl = metas[st]["label"]
            sec_headers += [f"{lbl}\nвремя", f"{lbl}\nтемп", f"{lbl}\nоткл"]
        sec_rows, sec_fill = [], {}
        for i, ser in enumerate(blk["series"], 1):
            row = [str(i)]
            col = 1
            for st in blk["steps"]:
                lap = ser.get(st)
                if lap:
                    m = metas[st]
                    et = (ar._seg_etalon(m, i) if m["role"] == "work"
                          else (sum(m["bounds"]) / 2.0 if m["bounds"] else None))
                    dev, color = _dev(lap["pace"], et)
                    row += [_fmt_time(lap["dur"]), _fmt_pace(lap["pace"]), dev]
                    if color:
                        sec_fill[(i, col + 2)] = _FILL[color]
                else:
                    row += ["—", "—", "—"]
                col += 3
            sec_rows.append(row)
        avg_row = ["ср."]
        for st in blk["steps"]:
            durs = [s[st]["dur"] for s in blk["series"] if st in s]
            dists = [s[st]["dist"] for s in blk["series"] if st in s and s[st]["dist"]]
            # Средний темп — взвешенный: Σвремя / Σдистанция (не среднее темпов).
            wpace = (sum(durs) / (sum(dists) / 1000)) if (durs and dists) else None
            avg_row += [_fmt_time(sum(durs) / len(durs)) if durs else "—",
                        _fmt_pace(wpace) if wpace else "—", ""]
        sec_rows.append(avg_row)
        work_lbls = [metas[st]["label"] for st in blk["steps"] if metas[st]["role"] == "work"]
        sec_title = f"{n_series} × (" + " + ".join(work_lbls) + ")"
        sections.append({"headers": sec_headers, "rows": sec_rows, "fill": sec_fill,
                         "avg_r": n_series + 1, "title": sec_title,
                         "n_rows": n_series + 2})

    # ── Шапка (зона 1) ──
    meta_bits = [b for b in (wdate, f"группа {wgroup}" if wgroup else None, source) if b]
    meta_line = "  ·  ".join(meta_bits)

    def _rcs_wrap(label, text, style):
        """Строки «Метка: текст» с переносом → [(строка, style), ...]; style: 'bold'|'italic'."""
        text = (text or "").strip()
        if not text:
            return []
        wrapped = textwrap.wrap(f"{label}: {text}", width=90, subsequent_indent="   ")
        return [(ln, style) for ln in wrapped]

    rcs_lines = []
    if s4:
        rcs_lines += _rcs_wrap("Работа", s4.get("work_text"), "bold")
        rcs_lines += _rcs_wrap("Цель", s4.get("overall_purpose"), "italic")
        rcs_lines += _rcs_wrap("Суть", s4.get("summary"), "italic")
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

    # ── Итоги (зона 3): средний темп взвешенный (Σвремя/Σдистанция) ──
    def _wavg(laps):
        d = sum(l["dist"] for l in laps if l["dist"])
        t = sum(l["dur"] for l in laps if l["dur"])
        return (t / (d / 1000)) if (d and t) else None

    work_avg = _wavg(work_laps)
    rest_avg = _wavg(rest_laps) if has_rest else None
    uniq_b = {tuple(p["bounds"]) for p in plan_steps
              if p.get("stype") == "interval" and p.get("bounds")}
    if len(uniq_b) == 1:
        s0, f0 = next(iter(uniq_b))
        work_goal = (_fmt_pace((s0 + f0) / 2) if abs(s0 - f0) <= ar.WORK_EXACT_EPS
                     else f"{_fmt_pace(s0)}→{_fmt_pace(f0)}")
    else:
        # Разные цели шагов → взвешенная по дистанции целевая (как и факт).
        def _pmid(st):
            p = next((q for q in plan_steps if q["idx"] == st), None)
            return (sum(p["bounds"]) / 2.0) if (p and p["bounds"]) else None
        md = [(_pmid(l["step"]), l["dist"]) for l in work_laps]
        md = [(m, d) for m, d in md if m and d]
        work_goal = ("≈ " + _fmt_pace(sum(m * d for m, d in md) / sum(d for _, d in md))
                     if md else "—")
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
    n_hdr_lines = 2 + len(rcs_lines) + (1 if plan_line else 0)
    hdr_in = 0.26 * n_hdr_lines + 0.18
    tbl_in = sum(0.46 * s["n_rows"] for s in sections) + \
        (0.14 * len(sections) if len(sections) > 1 else 0)
    tot_in = 1.05
    diag_in = 1.08
    fig_h = hdr_in + diag_in + tbl_in + tot_in + 0.6
    fig_w = 9.0
    with plt.rc_context(ar._rc()):
        fig = plt.figure(figsize=(fig_w, fig_h))
        gs = fig.add_gridspec(4, 1, height_ratios=[hdr_in, diag_in, tbl_in, tot_in],
                              hspace=0.04, left=0.03, right=0.97, top=0.985, bottom=0.02)
        ax_h = fig.add_subplot(gs[0]); ax_h.axis("off")
        ax_d = fig.add_subplot(gs[1])
        _plan_diagram(ax_d, blocks, plan_steps)
        sub = gs[2].subgridspec(len(sections), 1,
                                hspace=0.16 if len(sections) > 1 else 0.0,
                                height_ratios=[s["n_rows"] for s in sections])
        ax_b = fig.add_subplot(gs[3]); ax_b.axis("off")

        # Зона 1: шапка
        y = 1.0
        dy = 1.0 / max(n_hdr_lines + 0.1, 4)
        ax_h.text(0, y, "РАЗБОР ТРЕНИРОВКИ", fontsize=20, fontweight="bold",
                  color=accent, va="top", ha="left", transform=ax_h.transAxes)
        ax_h.text(1, y, name or "", fontsize=13, fontweight="bold",
                  va="top", ha="right", transform=ax_h.transAxes)
        y -= dy * 1.25
        ax_h.text(0, y, meta_line, fontsize=10.5, alpha=0.8,
                  va="top", ha="left", transform=ax_h.transAxes)
        y -= dy
        for ln, st in rcs_lines:
            ax_h.text(0, y, ln, fontsize=10.5,
                      style="italic" if st == "italic" else "normal",
                      fontweight="bold" if st == "bold" else "normal",
                      va="top", ha="left", transform=ax_h.transAxes)
            y -= dy
        if plan_line:
            ax_h.text(0, y, "ПЛАН: " + plan_line, fontsize=11, fontweight="bold",
                      va="top", ha="left", transform=ax_h.transAxes)

        # Зона 2: секции-таблицы по блокам (bbox на всю под-зону)
        zebra = "#2a2a2a" if dark else "#f2f2f2"
        hdr_bg = "#333333" if not dark else "#3a3a3a"
        for si, sec in enumerate(sections):
            ax_t = fig.add_subplot(sub[si]); ax_t.axis("off")
            if len(sections) > 1:
                ax_t.set_title(sec["title"], fontsize=10.5, fontweight="bold", pad=3)
            tbl = ax_t.table(cellText=sec["rows"], colLabels=sec["headers"],
                             cellLoc="center", bbox=[0, 0, 1, 1])
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(10)
            for (r, c), cell in tbl.get_celld().items():
                cell.set_edgecolor("#999999")
                if r == 0:
                    cell.set_facecolor(hdr_bg)
                    cell.set_text_props(fontweight="bold", color="white")
                elif r == sec["avg_r"]:
                    cell.set_facecolor(th["box_face"])
                    cell.set_text_props(fontweight="bold", color=th["text"])
                else:
                    cell.set_facecolor(zebra if r % 2 == 0 else "none")
                    cell.set_text_props(color=th["text"])
            for (r, c), fill in sec["fill"].items():
                tbl[r, c].set_facecolor(fill)
                tbl[r, c].set_text_props(color="white", fontweight="bold")

        # Зона 3: плашки итогов
        ax_b.set_xlim(0, 1); ax_b.set_ylim(0, 1)
        k = len(plaques)
        pw = min(0.22, 0.84 / k - 0.03)
        gap = (1.0 - k * pw) / (k + 1)
        for idx, (title, big, small, color) in enumerate(plaques):
            x0 = gap + idx * (pw + gap)
            ax_b.add_patch(FancyBboxPatch((x0, 0.14), pw, 0.72,
                           boxstyle="round,pad=0.008", linewidth=1.6,
                           edgecolor=color, facecolor="none",
                           transform=ax_b.transAxes))
            cx = x0 + pw / 2
            ax_b.text(cx, 0.72, title, fontsize=8.5, fontweight="bold", color=color,
                      ha="center", va="center", transform=ax_b.transAxes)
            ax_b.text(cx, 0.48, big, fontsize=15, fontweight="bold", color=color,
                      ha="center", va="center", transform=ax_b.transAxes)
            ax_b.text(cx, 0.26, small, fontsize=8.5, alpha=0.85,
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
