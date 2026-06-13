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
            and "DD_" in str(a.get("activityName") or "")]
    if selector is None:
        return runs[0] if runs else None
    if str(selector).isdigit():
        return next((a for a in (acts or []) if str(a.get("activityId")) == str(selector)), None)
    return next((a for a in runs if str(selector) in str(a.get("activityName") or "")), None)


async def build_package(db_user_id: int, selector=None) -> dict:
    """Собирает пакет данных для ИИ по DD-активности.
    selector: None → последняя DD; маска 'DD_YYYYMMDD'; либо activityId.
    Возвращает {ok, msg, name, text}. text — пакет без промпта (PROMPT добавляет вызывающий)."""
    client = await garmin._client(db_user_id)
    if not client:
        return {"ok": False, "msg": "Garmin не подключён или нет клиента."}

    acts = await asyncio.to_thread(client.get_activities, 0, 20)
    act = _pick_activity(acts, selector)
    if not act:
        sel = f" по «{selector}»" if selector else ""
        return {"ok": False, "msg": f"DD-активность{sel} не найдена в последних 20."}

    act_id = act.get("activityId")
    name = act.get("activityName")
    wkt_id = act.get("workoutId")
    m = re.search(r"DD_(\d{8})", name or "")
    wdate = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}" if m else None

    splits = await asyncio.to_thread(client.get_activity_splits, act_id)
    plan_steps = []
    if wkt_id:
        try:
            wkt = await asyncio.to_thread(client.get_workout_by_id, wkt_id)
            plan_steps = ar._flatten_plan_steps(wkt)
        except Exception:
            plan_steps = []
    try:
        details = await asyncio.to_thread(client.get_activity_details, act_id, 100000, 100000)
    except Exception:
        details = None
    pts = _parse_details(details)

    prof = db.get_user_profile(db_user_id) or {}
    snap = db.get_morning_caught(db_user_id)
    s4 = _s4_by_date(wdate, act.get("activityType", {}).get("typeKey"))
    rows, S = _enrich_laps(splits, plan_steps, pts)

    L = []
    A = L.append
    A("=" * 64)
    A("ПАКЕТ ДАННЫХ ДЛЯ АНАЛИЗА ТРЕНИРОВКИ")
    A("=" * 64)
    A(f"Тренировка: {name}")
    A(f"Дата: {act.get('startTimeLocal')}   activityId: {act_id}")

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

    A("\n[ПЛАН] (эталон из Garmin workout)")
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

    return {"ok": True, "name": name, "text": "\n".join(L), "msg": ""}


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
