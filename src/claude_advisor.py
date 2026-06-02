import os
import json
import math
import html as _html
import time as _time
import logging
from openai import OpenAI
from dotenv import load_dotenv
from version import VERSION

load_dotenv()

logger = logging.getLogger(__name__)

MODEL_DEEP  = "deepseek-v4-pro"    # thinking=True  — 🧠 Глубокое
MODEL_SMART = "deepseek-v4-flash"  # thinking=True  — ⚡ Умное
MODEL_FAST  = "deepseek-v4-flash"  # thinking=False — 🔥 Быстрое

_MODE_LABELS = {"deep": "🧠 Глубокий (ИИ)", "smart": "⚡ Быстрый (ИИ)",
                "fast": "🪶 Лёгкий (ИИ)", "calc": "📊 Расчётный",
                "b_ai": "🧪 B (ИИ выбор)"}


def _get_client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
        timeout=300.0,  # максимальный из всех режимов (deep=300s)
    )

last_prompt: str = ""


def _estimate_vo2max(fitness: dict, recovery: dict | None) -> tuple[float | None, str]:
    """
    Возвращает (vo2max, source).
    Приоритет: явное поле из устройства → оценка из Strava по VDOT (Jack Daniels).
    """
    if recovery:
        v = recovery.get("vo2max")
        if v:
            src = recovery.get("source", "устройство").capitalize()
            return float(v), src

    if fitness.get("vo2max"):
        src = fitness.get("vo2max_source", "устройство")
        return float(fitness["vo2max"]), src

    # Оценка по формуле VDOT из прогнозных времён Strava
    predictions = fitness.get("predictions", {})
    dist_map = {"5km": 5000, "10km": 10000, "1 mile": 1609}
    for dist_name, dist_m in dist_map.items():
        p = predictions.get(dist_name)
        if not p:
            continue
        parts = p["time"].split(":")
        try:
            t_sec = (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                     if len(parts) == 3 else int(parts[0]) * 60 + int(parts[1]))
        except (ValueError, IndexError):
            continue
        if t_sec <= 0:
            continue
        t_min = t_sec / 60
        v = dist_m / t_min  # m/min
        vo2 = -4.6 + 0.182258 * v + 0.000104 * v ** 2
        pct = (0.8 + 0.1894393 * math.exp(-0.012778 * t_min)
               + 0.2989558 * math.exp(-0.1932605 * t_min))
        if pct <= 0:
            continue
        return round(vo2 / pct, 1), f"Strava/{dist_name} (оценка)"

    return None, ""


def build_evening_prompt(workout: dict, fitness: dict, recovery: dict | None = None, weather_prompt: str = "") -> str:
    workout_type = workout.get("workout_type", "unknown")
    work_text = workout.get("work_text", "—")
    groups_raw = workout.get("groups_raw", "—")
    total_volume = workout.get("total_volume_km", "—")

    # Extra groups из комментариев сразу после основных групп
    extra_groups_raw = workout.get("extra_groups_raw", [])
    extra_section = ""
    if extra_groups_raw:
        extra_section = "\n\nДОПОЛНИТЕЛЬНЫЕ ГРУППЫ (из комментариев):\n" + \
            "\n---\n".join(r[:400] for r in extra_groups_raw[:4])

    # VO2max и лактатный порог
    vo2max, vo2max_source = _estimate_vo2max(fitness, recovery)
    if vo2max:
        level = (
            "начинающий" if vo2max < 35 else
            "любительский" if vo2max < 45 else
            "продвинутый любитель" if vo2max < 55 else
            "спортивный" if vo2max < 65 else "элитный"
        )
        vo2max_line = f"VO2max: {vo2max} мл/кг/мин [{vo2max_source}] — уровень: {level}"
    else:
        vo2max_line = "VO2max: нет данных"

    lt_pace = fitness.get("lactate_threshold_pace")
    lt_hr   = fitness.get("lactate_threshold_hr")
    if lt_pace:
        lt_line = f"Лактатный порог: {lt_pace} мин/км"
        if lt_hr:
            lt_line += f" при ЧСС {lt_hr} уд/мин"
    else:
        lt_line = ""

    # Прогнозные времена
    predictions = fitness.get("predictions", {})
    pred_lines = []
    for d in ["5km", "10km", "Half-Marathon"]:
        p = predictions.get(d)
        if p:
            src = "факт" if p.get("source") == "actual" else "прогноз"
            pred_lines.append(f"  {d}: {p['time']} ({p['pace']} мин/км) [{src}]")
    pred_text = ("Прогнозные времена:\n" + "\n".join(pred_lines)) if pred_lines else ""

    # Нагрузка CTL/ATL/TSB или Training Load (Garmin)
    load = fitness.get("training_load", {})
    if load:
        load_src = load.get("source", "strava")
        load_summary = load.get("summary", "")
        if load_src == "garmin" and load_summary:
            load_text = f"Training Load (Garmin): {load_summary}"
        else:
            load_text = load_summary
    else:
        load_text = ""

    # Восстановление
    recovery_parts = []
    if recovery:
        src = recovery.get("source", "")
        if src == "garmin":
            if recovery.get("body_battery") is not None:
                recovery_parts.append(f"Body Battery: {recovery['body_battery']}/100 [Garmin]")
            if recovery.get("hrv") is not None:
                recovery_parts.append(f"HRV: {recovery['hrv']} мс [Garmin]")
            tr = recovery.get("training_readiness")
            if tr and tr.get("score") is not None:
                recovery_parts.append(f"Training Readiness: {tr['score']}/100 ({tr.get('level', '')})")
        else:
            if recovery.get("recovery_score") is not None:
                recovery_parts.append(f"Recovery Score: {recovery['recovery_score']}% [Whoop]")
            if recovery.get("hrv"):
                recovery_parts.append(f"HRV: {recovery['hrv']} мс")
            if recovery.get("sleep_hours"):
                recovery_parts.append(f"Сон: {recovery['sleep_hours']} ч")
            if recovery.get("body_battery") is not None:
                recovery_parts.append(f"Body Battery: {recovery['body_battery']}")
    recovery_line = ("Восстановление: " + ", ".join(recovery_parts)) if recovery_parts else ""

    # Соревнование
    last_race = fitness.get("last_race")
    race_text = ""
    if last_race and last_race.get("still_recovering"):
        race_text = (
            f"ВНИМАНИЕ: {last_race['days_since']} дней назад — "
            f"{last_race['name']} ({last_race['distance_km']} км). "
            f"Восстановление до {last_race['recovery_until']}. Нужен щадящий режим!"
        )

    type_label = {
        'interval': 'интервальную тренировку',
        'long': 'длительную тренировку (100 мин)',
        'hills': 'тренировку в гору',
        'unknown': 'тренировку',
    }.get(workout_type, 'тренировку')

    gender = fitness.get("gender")
    gender_line = ""
    if gender == "male":
        gender_line = "Пол: мужской (нормы VO2max для мужчин, восстановление стандартное)"
    elif gender == "female":
        gender_line = "Пол: женский (нормы VO2max для женщин ниже на ~10%, учти при интерпретации уровня)"

    profile_only = fitness.get("profile_only", False)
    profile_only_note = (
        "\n⚠️ ВАЖНО: Данные только из профиля пользователя — нет данных о текущей нагрузке "
        "и восстановлении. Рекомендация основана только на VO2max и лактатном пороге. "
        "Дай наиболее консервативную безопасную рекомендацию.\n"
    ) if profile_only else ""

    parts = [
        "Отвечай последовательно и детерминированно. При одинаковых входных данных давай одинаковый ответ.",
        "",
        f"Ты тренер бегового клуба Dusty Dumbbells. Помоги участнику выбрать группу для завтрашней {type_label}.",
        profile_only_note,
        f"ТРЕНИРОВКА: {workout.get('workout_date', '—')}  |  {workout.get('location', '—')}",
        f"Объём: {total_volume}",
        "",
        "РАБОТА:",
        work_text,
        "",
        "ГРУППЫ:",
        groups_raw + extra_section,
        "",
        "ДАННЫЕ СПОРТСМЕНА:",
        vo2max_line,
    ]
    if gender_line:
        parts.append(gender_line)
    if lt_line:
        parts.append(lt_line)
    if pred_text:
        parts.append(pred_text)
    if load_text:
        parts.append(f"Нагрузка: {load_text}")
    if recovery_line:
        parts.append(recovery_line)
    if fitness.get("summary"):
        parts.append(fitness["summary"])

    # Острая нагрузка за 48 ч — всегда свежие данные
    load_48h = fitness.get("load_48h")
    if load_48h and load_48h.get("sessions_48h", 0) > 0:
        h = load_48h.get("last_activity_hours_ago")
        last_str = f"{h} ч назад" if h is not None else "недавно"
        suffer = load_48h.get("suffer_48h") or 0
        km_48h = load_48h.get("total_km_48h", load_48h.get("km_48h", "?"))

        # Оцениваем интенсивность по suffer_score; если недоступен — по темпу vs лактатный порог
        if suffer > 0:
            if suffer < 50:
                intensity_note = f"suffer score {suffer} — восстановительная нагрузка, не влияет на выбор группы"
            elif suffer <= 150:
                intensity_note = f"suffer score {suffer} — умеренная нагрузка, учти при выборе группы"
            else:
                intensity_note = f"suffer score {suffer} — тяжёлая нагрузка, снизь рекомендуемую группу"
        else:
            avg_pace_48h = load_48h.get("avg_pace")
            if avg_pace_48h and lt_pace:
                def _p2s(p: str) -> int | None:
                    try:
                        m, s = p.split(":")
                        return int(m) * 60 + int(s)
                    except Exception:
                        return None
                p_sec = _p2s(avg_pace_48h)
                lt_sec = _p2s(lt_pace)
                if p_sec and lt_sec:
                    if p_sec < lt_sec * 0.97:
                        intensity_note = f"темп {avg_pace_48h} мин/км — выше ПАНО, тяжёлая нагрузка, снизь группу"
                    elif p_sec < lt_sec * 1.05:
                        intensity_note = f"темп {avg_pace_48h} мин/км — около ПАНО, умеренная нагрузка"
                    else:
                        intensity_note = f"темп {avg_pace_48h} мин/км — ниже ПАНО, восстановительная нагрузка"
                else:
                    intensity_note = "интенсивность недоступна"
            else:
                intensity_note = "интенсивность недоступна (нет suffer score и данных о темпе)"

        parts += [
            "",
            "НАГРУЗКА ЗА ПОСЛЕДНИЕ 48 ЧАСОВ:",
            f"Тренировок: {load_48h.get('sessions_48h', '?')}, "
            f"Километраж: {km_48h} км, "
            f"Последняя: {last_str}",
            f"Интенсивность: {intensity_note}.",
        ]

    if race_text:
        parts.append(f"\n{race_text}")

    lt_instruction = (
        "\n   — Лактатный порог указан в профиле — учти его при выборе: темп основной группы\n"
        "     должен быть близок к пороговому или чуть ниже."
    ) if lt_line else ""

    parts += [
        "",
        "АЛГОРИТМ ВЫБОРА ГРУППЫ:",
        "ТИП ИНТЕРВАЛЬНОЙ РАБОТЫ влияет на выбор группы:",
        "- Длинные отрезки (≥1 км): темп должен быть на уровне ПАНО или чуть ниже. Ориентир — лактатный порог.",
        "- Короткие отрезки (200-400м): работа выше ПАНО допустима и является целью. Ориентир — время на отрезке относительно личного рекорда на этой дистанции.",
        "- Смешанная работа (длинный + короткие): длинный отрезок около ПАНО, короткие выше ПАНО.",
        "Определи тип работы из раздела РАБОТА и ГРУППЫ перед выбором группы.",
        "",
        "ВАЖНО про темп: темп указывается в мин:сек на км. Меньшее число = БЫСТРЕЕ.",
        f"Темп 3:45 мин/км БЫСТРЕЕ чем 4:17 мин/км (лактатный порог).",
        "Если темп группы МЕНЬШЕ порогового — работа ВЫШЕ ПАНО (анаэробная зона, высокая интенсивность).",
        "Если темп группы БОЛЬШЕ порогового — работа НИЖЕ ПАНО (аэробная зона, умеренная интенсивность).",
        "Используй это при оценке подходимости групп и расчёте suitability_percentages.",
        "",
        "ВАЖНО про прогрессию темпа:",
        "Темп 4:05 → 3:40 — это УСКОРЕНИЕ (спортсмен бежит БЫСТРЕЕ).",
        "Темп уменьшается в минутах = скорость увеличивается.",
        "НЕ писать 'темп снижается' когда цифры уменьшаются — писать 'темп растёт' или 'ускорение'.",
        "Правильно: 'каждые 2 отрезка темп растёт: 4:05 → 4:00 → 3:55 → 3:50 → 3:45 → 3:40'",
        "Неправильно: 'темп снижается: 4:05 → 3:40'",
        "",
        "АНАЛИЗ ВОССТАНОВИТЕЛЬНЫХ ОТРЕЗКОВ:",
        "Формат '300м – 1:30' означает ВРЕМЯ прохождения отрезка, не темп.",
        "Расчёт темпа: время_в_секундах / дистанция_в_км = секунд на км → переведи в мин:сек.",
        "Пример: '300м – 1:30' → 90 сек / 0.3 км = 300 сек/км = 5:00 мин/км.",
        "",
        "Формат '500м – 1:52-1:42' = диапазон времени:",
        "медленный: 112 сек / 0.5 км = 224 сек/км = 3:44 мин/км",
        "быстрый: 102 сек / 0.5 км = 204 сек/км = 3:24 мин/км",
        "",
        "Если темп восстановления быстрее 5:30 мин/км — активное восстановление.",
        "Укажи рассчитанный темп в поле warning: 'темп ~X:XX мин/км'.",
        "Учти в suitability_percentages: нагрузка группы выше на 15%.",
        "",
        "ЗАДАЧА: дай рекомендацию по группе с учётом ощущений на разминке.",
        "",
        *(["ПОГОДА НА ТРЕНИРОВКУ:", weather_prompt,
           "Учитывай погоду при выборе группы и советах:",
           "- Жара (>25°C): снизить темп на 10-20 сек/км, советовать пить каждые 15 мин",
           "- Холод (<5°C): удлинить разминку, мышцы скованнее — первые км медленнее",
           "- Дождь или ветер >8 м/с: скорректировать ожидаемый темп +5-15 сек/км",
           "- Жара + усталость (TSB < -20): дополнительный повод снизить группу",
           ""] if weather_prompt else []),
        "Правила:",
        f"1. Выбери ОСНОВНУЮ группу по VO2max и прогнозным темпам.{lt_instruction}",
        "2. Укажи что делать если ноги бегут легко на разминке (группа выше).",
        "3. Укажи что делать если разминка идёт тяжело (группа ниже).",
        "4. АНАЛИЗ ТЕМПОВОГО РАЗРЫВА между основной группой и соседними:",
        "   — Вычисли разницу основного рабочего темпа между соседними группами.",
        "   — Если разрыв > 25 сек/км — это БОЛЬШОЙ разрыв: предложи промежуточный темп",
        "     (группа X.5) как среднее двух групп с конкретными цифрами.",
        "   — Если разрыв ≤ 25 сек/км — скажи что переход к соседней группе комфортен.",
        "5. В suitability_percentages оцени подходимость КАЖДОЙ группы из раздела ГРУППЫ",
        "   (включая дополнительные). 100 = идеально, 0 = совсем не подходит.",
        "   Группа с максимальным процентом ДОЛЖНА совпадать с recommended_group.",
        "   ВАЖНО: поле 'group' в suitability_percentages — ТОЛЬКО номер группы.",
        "   Допустимо: '1', '2', '3', '3.5', '4', '5'. ЗАПРЕЩЕНО: 'Группа 3', '3 быстрая', '5 (537м)'.",
        "6. garmin_workout: сгенерируй JSON тренировки для Garmin Connect API.",
        f"   workoutName: \"DD_{workout.get('workout_date','').replace('-','')}-{{recommended_group}}_lvl\"",
        "   Скорость в м/с: 1000/(M*60+SS) для темпа M:SS мин/км.",
        "   Если темп задан как время на отрезок (напр. '2:10 на 500 м') → 500/(2*60+10).",
        "   targetValueOne = БЫСТРЕЕ (большее м/с), targetValueTwo = МЕДЛЕННЕЕ.",
        "   Рабочие отрезки: stepTypeKey='interval', endCondition='distance' (метры), targetType='pace.zone'.",
        "   Восстановление: stepTypeKey='recovery'.",
        "   Если время задано явно (напр. '300м — 1:30') → targetType='pace.zone',",
        "   targetValueOne=targetValueTwo=dist_m/total_sec (напр. 300/90=3.3333333 м/с).",
        "   Если времени нет ('200м лёгкий бег') → targetType='no.target', targetValueOne/Two=null.",
        "   Повторы: RepeatGroupDTO с numberOfIterations.",
        "   stepOrder: сквозная нумерация с 1. У шагов внутри RepeatGroup childStepId=1, у остальных null.",
        "",
        "Дай ответ строго в формате JSON:",
        """{
  "recommended_group": "номер (например: 3)",
  "recommended_pace": "темп основной группы (например: 4:00–4:25 мин/км)",
  "reason": "1-2 предложения: почему эта группа — укажи VO2max и темп",
  "if_feeling_good": "что делать если ноги бегут легко — группа выше с темпом, пометь если разрыв большой",
  "if_tired": "что делать если устал — группа ниже или промежуточный темп X.5 с цифрами",
  "gap_note": "вывод по разрывам: небольшой (можно переходить) или большой (нужна промежуточная)",
  "suitability_percentages": [
    {"group": "номер группы", "percentage": число_от_0_до_100, "comment": "СТРОГО 1-2 слова (примеры: идеально, запасной, быстро, легко, опасно, на грани)"}
  ],
  "preparation_tips": ["совет 1", "совет 2"],
  "warning": "предупреждение или null",
  "garmin_workout": {
    "workoutName": "DD_YYYYMMDD-GROUP_lvl",
    "description": null,
    "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "subSportType": null,
    "workoutSegments": [{"segmentOrder": 1, "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}, "poolLengthUnit": null, "poolLength": null, "workoutSteps": [
      {
        "type": "RepeatGroupDTO",
        "stepOrder": 1,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
        "childStepId": null,
        "numberOfIterations": 12,
        "workoutSteps": [
          {"type": "ExecutableStepDTO", "stepOrder": 2, "stepType": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3}, "childStepId": 1, "description": null, "endCondition": {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": true}, "endConditionValue": 500.0, "preferredEndConditionUnit": null, "endConditionCompare": null, "targetType": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}, "targetValueOne": 3.8461538, "targetValueTwo": 3.6496351, "targetValueUnit": null, "zoneNumber": null, "secondaryTargetType": null, "secondaryTargetValueOne": null, "secondaryTargetValueTwo": null, "secondaryTargetValueUnit": null, "secondaryZoneNumber": null, "endConditionZone": null, "strokeType": {"strokeTypeId": 0, "strokeTypeKey": null, "displayOrder": 0}, "equipmentType": {"equipmentTypeId": 0, "equipmentTypeKey": null, "displayOrder": 0}, "category": null, "exerciseName": null, "workoutProvider": null, "providerExerciseSourceId": null, "weightValue": null, "weightUnit": null},
          {"type": "ExecutableStepDTO", "stepOrder": 3, "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4}, "childStepId": 1, "description": null, "endCondition": {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": true}, "endConditionValue": 300.0, "preferredEndConditionUnit": null, "endConditionCompare": null, "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}, "targetValueOne": null, "targetValueTwo": null, "targetValueUnit": null, "zoneNumber": null, "secondaryTargetType": null, "secondaryTargetValueOne": null, "secondaryTargetValueTwo": null, "secondaryTargetValueUnit": null, "secondaryZoneNumber": null, "endConditionZone": null, "strokeType": {"strokeTypeId": 0, "strokeTypeKey": null, "displayOrder": 0}, "equipmentType": {"equipmentTypeId": 0, "equipmentTypeKey": null, "displayOrder": 0}, "category": null, "exerciseName": null, "workoutProvider": null, "providerExerciseSourceId": null, "weightValue": null, "weightUnit": null}
        ],
        "endConditionValue": 12.0, "preferredEndConditionUnit": null, "endConditionCompare": null,
        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": false},
        "skipLastRestStep": false, "smartRepeat": false
      }
    ]}]
  }
}""",
        "",
        "Отвечай только JSON, без лишнего текста.",
    ]

    return "\n".join(parts)


def _build_recovery_block(source, recovery_score, hrv, sleep_score, sleep_hours, body_battery, training_readiness) -> str:
    lines = []
    if source == 'garmin':
        lines.append(f"- Body Battery: {body_battery if body_battery is not None else '—'}/100")
        lines.append(f"- HRV (прошлая ночь): {hrv if hrv is not None else '—'} мс")
        if training_readiness:
            score = training_readiness.get('score', '—')
            level = training_readiness.get('level', '')
            lines.append(f"- Training Readiness: {score}/100 ({level})")
            factors = training_readiness.get('factors') or []
            if factors:
                lines.append(f"- Факторы, снижающие готовность: {', '.join(str(f) for f in factors[:4])}")
    else:
        lines.append(f"- Whoop Recovery Score: {recovery_score if recovery_score is not None else '—'}%")
        lines.append(f"- HRV: {hrv if hrv is not None else '—'} мс")
        lines.append(f"- Качество сна: {sleep_score if sleep_score is not None else '—'}%")
        lines.append(f"- Часов сна: {sleep_hours if sleep_hours is not None else '—'}")
        if body_battery is not None:
            lines.append(f"- Garmin Body Battery: {body_battery}")
        if training_readiness:
            tr_score = training_readiness.get('score', '—')
            tr_level = training_readiness.get('level', '')
            lines.append(f"- Garmin Training Readiness: {tr_score}/100 ({tr_level})")
            tr_factors = training_readiness.get('factors') or []
            if tr_factors:
                lines.append(f"- Факторы: {', '.join(str(f) for f in tr_factors[:3])}")
    return "\n".join(lines)


def build_morning_prompt(workout: dict, fitness: dict, recovery: dict, last_rec: dict | None = None, weather_prompt: str = "") -> str:
    extra_groups = workout.get("extra_groups", [])
    extra_groups_raw = workout.get("extra_groups_raw", [])
    extra_section = ""
    if extra_groups or extra_groups_raw:
        extra_nums = ", ".join(g["number"] for g in extra_groups if g.get("number"))
        header = "\n\nДОПОЛНИТЕЛЬНЫЕ ГРУППЫ (из комментариев)"
        if extra_nums:
            header += f" — группы {extra_nums}"
        header += ":"
        extra_section = header
        if extra_groups_raw:
            extra_section += "\n" + "\n---\n".join(r[:400] for r in extra_groups_raw[:4])
    groups_text = workout.get("groups_raw", "—") + extra_section

    recovery_source = recovery.get('source', 'whoop')
    recovery_score = recovery.get('recovery_score')
    hrv = recovery.get('hrv')
    sleep_hours = recovery.get('sleep_hours')
    sleep_score = recovery.get('sleep_score')
    body_battery = recovery.get('body_battery')
    training_readiness = recovery.get('training_readiness')  # Garmin: {score, level, factors}
    hrv_status = recovery.get('hrv_status')  # Garmin: {hrv_weekly_avg, status}

    # Уровень восстановления
    if recovery_source == 'garmin':
        tr_score = training_readiness.get('score') if training_readiness else None
        if tr_score is not None:
            if tr_score >= 70:
                recovery_level = "хорошее (Training Readiness высокий)"
            elif tr_score >= 40:
                recovery_level = "умеренное (Training Readiness средний)"
            else:
                recovery_level = "плохое (Training Readiness низкий)"
        elif body_battery is not None:
            if body_battery >= 70:
                recovery_level = "хорошее (Body Battery высокий)"
            elif body_battery >= 40:
                recovery_level = "умеренное (Body Battery средний)"
            else:
                recovery_level = "плохое (Body Battery низкий)"
        else:
            recovery_level = "неизвестно"
    elif recovery_score is not None:
        if recovery_score >= 67:
            recovery_level = "хорошее (зелёная зона Whoop)"
        elif recovery_score >= 34:
            recovery_level = "умеренное (жёлтая зона Whoop)"
        else:
            recovery_level = "плохое (красная зона Whoop)"
    elif body_battery is not None:
        if body_battery >= 70:
            recovery_level = "хорошее"
        elif body_battery >= 40:
            recovery_level = "умеренное"
        else:
            recovery_level = "плохое"
    else:
        recovery_level = "неизвестно"

    load = fitness.get("training_load", {})
    load_text = load.get("summary", "") if load else ""

    predictions = fitness.get("predictions", {})
    pred_text = ""
    if predictions:
        key_dists = ["5km", "10km"]
        pred_lines = []
        for d in key_dists:
            if d in predictions:
                p = predictions[d]
                pred_lines.append(f"  {d}: {p['time']} ({p['pace']} мин/км)")
        if pred_lines:
            pred_text = "Прогнозы: " + ", ".join(pred_lines)

    last_race = fitness.get("last_race")
    race_text = ""
    if last_race and last_race.get("still_recovering"):
        race_text = (
            f"Спортсмен восстанавливается после соревнования "
            f"({last_race['name']}, {last_race['distance_km']} км, "
            f"{last_race['days_since']} дней назад). "
            f"Восстановление до {last_race['recovery_until']}."
        )

    if last_rec:
        last_rec_section = (
            f"\nВЕЧЕРНЯЯ РЕКОМЕНДАЦИЯ (вчера/накануне):\n"
            f"Группа: {last_rec.get('recommended_group', '—')}\n"
            f"Темп: {last_rec.get('recommended_pace', '—')}\n"
            f"Обоснование: {last_rec.get('reason', '—')}\n"
            f"Если легко на разминке: {last_rec.get('if_feeling_good', '—')}\n"
            f"Если тяжело: {last_rec.get('if_tired', '—')}\n"
        )
    else:
        last_rec_section = "\nВЕЧЕРНЯЯ РЕКОМЕНДАЦИЯ: нет данных (спортсмен не запрашивал /workout накануне).\n"

    return f"""Ты тренер бегового клуба. Спортсмен собирается на тренировку через 1-2 часа.
Вечером ты уже рекомендовал ему группу. Теперь проверь утренние данные восстановления и скорректируй план если нужно.
{last_rec_section}
ТРЕНИРОВКА СЕГОДНЯ:
Тип: {workout.get('workout_type', '—')}
Место: {workout.get('location', '—')}
Расписание: {workout.get('schedule', '—')}

ГРУППЫ:
{groups_text}

ДАННЫЕ ВОССТАНОВЛЕНИЯ ЗА НОЧЬ (источник: {recovery_source.upper()}):
{_build_recovery_block(recovery_source, recovery_score, hrv, sleep_score, sleep_hours, body_battery, training_readiness)}
- Общий уровень восстановления: {recovery_level}

ДАННЫЕ НАГРУЗКИ (Strava):
{fitness.get('summary', '—')}
{f"{chr(10)}{load_text}" if load_text else ""}
{f"{chr(10)}{pred_text}" if pred_text else ""}
{f"{chr(10)}СОРЕВНОВАНИЕ: {race_text}" if race_text else ""}
{(chr(10) + "ПОГОДА НА ТРЕНИРОВКУ:" + chr(10) + weather_prompt + chr(10) + "Учитывай: жара >25°C → темп медленнее; холод <5°C → длиннее разминка; дождь/ветер >8 м/с → скорректировать темп.") if weather_prompt else ""}

Дай ответ строго в формате JSON:
{{
  "status": "go" | "adjust" | "rest",
  "recommended_group": "номер группы",
  "message": "короткое сообщение спортсмену (2-3 предложения, дружески и по делу)",
  "adjustments": ["конкретный совет по тренировке сегодня (или null если всё хорошо)"]
}}

Статусы:
- "go": всё хорошо, идти по плану
- "adjust": идти, но снизить нагрузку (другая группа или темп)
- "rest": лучше пропустить или сделать очень лёгкую пробежку

ВАЖНО: главный показатель готовности зависит от источника.
Whoop Recovery Score: >= 67% → "go", >= 34% → "adjust", < 34% → "rest".
Garmin Training Readiness: >= 70 → "go", >= 40 → "adjust", < 40 → "rest".
Garmin Body Battery (если нет Training Readiness): >= 70 → "go", >= 40 → "adjust", < 40 → "rest".
Если спортсмен восстанавливается после соревнования — статус не выше "adjust".

Отвечай только JSON, без лишнего текста."""


def _build_analyze_prompt(raw_text: str, comments_text: str) -> str:
    """Промпт для предварительного анализа анонса тренировки."""
    return (
        "Ты анализируешь анонс тренировки бегового клуба Dusty Dumbbells.\n\n"
        "ТЕКСТ ПОСТА:\n"
        f"{raw_text}\n\n"
        "КОММЕНТАРИИ К ПОСТУ:\n"
        f"{comments_text}\n\n"
        "ЗАДАЧА 1 — Валидация:\n"
        "Определи, является ли это РЕАЛЬНЫМ анонсом будущей тренировки.\n"
        "Анонс: есть дата, расписание (сбор/старт), описание работы С ГРУППАМИ И ТЕМПАМИ.\n"
        "НЕ анонс (is_valid=false): фото-отчёт о прошедшей ('вчера провели', 'спасибо команде'),\n"
        "новости клуба, реклама, а также пост где есть дата и место, но НЕТ темпов и групп\n"
        "(анонс следующей тренировки без структуры, поздравление с упоминанием даты).\n"
        "Во всех случаях is_valid=false укажи reject_reason.\n\n"
        "ЗАДАЧА 2 — Тип тренировки:\n"
        "'interval' — вт/пт, отрезки (200м, 500м, 1км и тд)\n"
        "'long' — воскресенье, Long Run 100 минут по темпу\n\n"
        "============ ДЛЯ INTERVAL-ТРЕНИРОВОК ============\n\n"
        "ЗАДАЧА 3 — СТРУКТУРА работы (structure[]):\n"
        "Структура задаётся ОДИН раз в основном посте (например\n"
        "'10 по 300/100 + 400 + 10 по 100/100') и одинакова для ВСЕХ групп —\n"
        "группы отличаются ТОЛЬКО темпами. Разбей работу на блоки по порядку\n"
        "(block: 1, 2, 3...). Два типа блоков:\n"
        "- 'repeat': блок повторов — поля reps, work_distance_m, recovery_distance_m, purpose\n"
        "- 'easy': одиночный лёгкий отрезок-разделитель — поля distance_m, description\n"
        "  (без повторов и без темпа)\n"
        "purpose — физиология блока НЕЙТРАЛЬНО, без жёсткой привязки к одной зоне.\n"
        "Опиши диапазон: какие качества блок задействует в зависимости от темпа.\n"
        "Пример: 'короткие быстрые отрезки с неполным восстановлением — в зависимости\n"
        "от темпа работают на МПК (быстрые группы) или скоростную выносливость/ПАНО\n"
        "(умеренные)'. Конкретную зону НЕ фиксируй — она определяется в рекомендации под\n"
        "пользователя.\n"
        "overall_purpose — что развивает тренировка целиком (1-2 предложения).\n\n"
        "ЗАДАЧА 3b — СВОЙСТВА ТРЕНИРОВКИ (только interval, верхний уровень):\n"
        "Это свойства самой тренировки, общие для ВСЕХ, НЕ зависят от пользователя.\n"
        "block_contrast — чем блоки отличаются друг от друга и зачем именно такой порядок\n"
        "  (физиологический смысл последовательности). Например: сначала длинные быстрые\n"
        "  300-ки на выносливость на свежих ногах, затем короткие 100-ки на скорость на фоне\n"
        "  усталости — учит технике и финишу уставшим.\n"
        "target_athlete — на кого рассчитана (уровень, к чему готовит). Например: для\n"
        "  подготовленных к 5-10км, развивающая.\n"
        "intensity_level — общая тяжесть: 'лёгкая' / 'средняя' / 'тяжёлая' / 'очень тяжёлая'.\n"
        "what_to_watch — на что обратить внимание, типичные ошибки. Например: не убегать\n"
        "  на первых 300-ках, держать технику на 100-ках.\n"
        "total_volume_km — общий объём в км числом (из строки 'Объём: ...'). Отдельным\n"
        "  полем, НЕ в coach_notes. Если не указан — null.\n"
        "is_borderline — даёт ли тренировка реальный выбор РАЗНЫХ качеств между группами:\n"
        "  true — разные группы развивают разные качества (напр на 300м быстрая группа =\n"
        "    МПК, медленная = ПАНО/темп);\n"
        "  false — группы отличаются только 'быстрее/медленнее по силам', характер работы\n"
        "    одинаковый для всех.\n"
        "borderline_note — краткое пояснение почему borderline (или null если false).\n"
        "НЕ добавляй сюда упрощение/подгонку под конкретного человека и под его усталость —\n"
        "это Шаг 2 (рекомендация), не сюда.\n\n"
        "ЗАДАЧА 4 — ГРУППЫ (groups[]):\n"
        "Каждая группа описывается ТОЛЬКО темпами по блокам, БЕЗ повторения структуры.\n"
        "groups[].blocks — массив {block: N, work_pace_start, work_pace_end, work_distance_m,\n"
        "recovery_pace, active_recovery}.\n"
        "ПРОГРЕССИЯ ТЕМПА (критично): если рабочий темп задан ДИАПАЗОНОМ\n"
        "(напр '600 м – 2.00–1.50 минуты' = прогрессия от 2:00 к 1:50 на отрезок):\n"
        "  work_pace_start = темп/км ПЕРВОГО (медленного) отрезка,\n"
        "  work_pace_end = темп/км ПОСЛЕДНЕГО (быстрого) отрезка.\n"
        "Если прогрессии НЕТ (единый темп) — work_pace_start == work_pace_end.\n"
        "НИКОГДА не сворачивай прогрессию в одно число и не усредняй —\n"
        "всегда сохраняй оба края отдельно. Оба в темпе мин:сек на км.\n"
        "work_distance_m — длина рабочего отрезка в метрах (из структуры этого блока;\n"
        "  у групп с нестандартной дорожкой — фактическая длина этой группы).\n"
        "Привязка к структуре через 'block': N. easy-блоки в groups НЕ дублируй\n"
        "(у них нет темпа — они только в structure).\n"
        "reps_override — если у группы меньше повторов чем в общей структуре\n"
        "(напр 10 вместо 12) — укажи число, иначе null.\n"
        "from_comment — true если группа пришла из комментария.\n"
        "  ВАЖНО: если группа из комментария имеет ПОЛНЫЙ набор отрезков с темпом —\n"
        "  она идёт в groups с from_comment=true (НЕ в extra_groups).\n"
        "track_note — пометка о нестандартной дорожке (напр 'адаптировано под 4-ю дорожку'\n"
        "  для группы 5 с дистанциями 322/430/108м вместо 300/400/100), иначе null.\n"
        "  Фактические дистанции группы сохраняй как есть.\n"
        "health_group — группа здоровья/ходоков (эмодзи ⏺️ или текст 'бег и ходьба'):\n"
        "  {number:'здоровье', blocks:[], reps_override:null, from_comment:..., track_note:null,\n"
        "   health_group:true}. Темп НЕ выдумывай; в summary отметь что это бег/ходьба\n"
        "  для начинающих и людей без данных профиля. У остальных групп health_group=false.\n\n"
        "ПРАВИЛА ВЫЧИСЛЕНИЯ ТЕМПА (для work_pace / recovery_pace в блоках):\n"
        "ФОРМАТ ВРЕМЕНИ (критично): точка = разделитель минут и секунд, НЕ дробь.\n"
        "'1.32' = 1 мин 32 сек = 92 сек. '1.10' = 70 сек. '1.22' = 82 сек.\n"
        "Целое без точки = секунды: '60 секунд' = 60 сек, '16 секунд' = 16 сек.\n"
        "Темп в скобках '(4:45/6:10)' = уже посчитанный клубом темп (работа/восст мин/км) —\n"
        "используй его если есть. Иначе пересчитай: 90сек / 0.3км = 5:00 мин/км.\n"
        "РАБОТА vs ВОССТАНОВЛЕНИЕ: '300 м – 60 сек / 100 м – 30 сек' = рабочий отрезок\n"
        "(300м за 60 сек, work_pace) + восстановительный (100м трусцой за 30 сек, recovery_pace).\n"
        "Первый (длиннее/быстрее) = work, второй (короче/медленнее) = recovery. Не путай местами.\n"
        "ACTIVE_RECOVERY (логика OR — достаточно ОДНОГО условия): active_recovery=true если\n"
        "ЛИБО темп восстановления быстрее 5:30/км, ЛИБО восстановление медленнее рабочего\n"
        "отрезка не более чем на ~50%. Иначе полный отдых — active_recovery=false.\n"
        "НЕТ ЦЕЛЕВОГО ТЕМПА: если у восстановления нет ни темпа, ни времени для пересчёта —\n"
        "active_recovery=false, recovery_pace=null. Не фантазируй и не оценивай на глаз.\n\n"
        "============ ДЛЯ LONG-ТРЕНИРОВОК ============\n\n"
        "ЗАДАЧА 3L — structure НЕ заполняй (structure=[]), overall_purpose=null.\n"
        "Группы опиши в groups[] СТАРЫМ форматом: {number, work (описание темпа по половинам),\n"
        "recovery: null, recovery_pace: null, active_recovery: false, blocks: [],\n"
        "from_comment: ..., track_note: null, reps_override: null, health_group: false}.\n"
        "Дополнительно заполни has_progression (вторая половина быстрее) и\n"
        "even_pace_available (есть ли опция ровного темпа).\n\n"
        "============ ОБЩЕЕ ============\n\n"
        "ЗАДАЧА 5 — Дополнительные группы (extra_groups[]):\n"
        "Сюда — ТОЛЬКО группы-ССЫЛКИ без своих отрезков: 'Группа 3.5 работает с 3 группой' →\n"
        '{"number": "3.5", "description": "работает с группой 3", "source": "комментарий"}.\n'
        "НЕ выдумывай для неё конкретные отрезки и темп.\n"
        "ИГНОРИРУЙ: индивидуальные советы ('кому отдохнуть'), соревнования с датами, 'переходим в'.\n"
        "Группы с ПОЛНЫМ набором отрезков (даже из комментария) → в groups, НЕ сюда.\n\n"
        "ЗАДАЧА 6 — coach_notes:\n"
        "coach_notes — только общие уточнения для ВСЕХ (маршрут, разминка, заминка).\n"
        "НЕ включай: подготовку к конкретным забегам с датами ('перед забегом 30.05'),\n"
        "индивидуальные рекомендации кому в какую группу. Это в ignored.\n\n"
        "ЗАДАЧА 7 — Дата:\n"
        "workout_date — используй текущий год 2026, если год не указан явно в посте.\n\n"
        "Верни строго JSON (пример для interval):\n"
        "{\n"
        '  "is_valid": true,\n'
        '  "reject_reason": null,\n'
        '  "workout_type": "interval",\n'
        '  "workout_date": "YYYY-MM-DD",\n'
        '  "summary": "суть тренировки 1-2 предложения",\n'
        '  "structure": [\n'
        '    {"block": 1, "type": "repeat", "reps": 10, "work_distance_m": 300, "recovery_distance_m": 100, "purpose": "быстрые отрезки с неполным восстановлением — МПК у быстрых групп или ПАНО/скоростная выносливость у умеренных"},\n'
        '    {"block": 2, "type": "easy", "distance_m": 400, "description": "лёгкий бег между блоками"},\n'
        '    {"block": 3, "type": "repeat", "reps": 10, "work_distance_m": 100, "recovery_distance_m": 100, "purpose": "короткие отрезки — скорость/нейромышечная активация, у умеренных групп акцент на технике"}\n'
        "  ],\n"
        '  "overall_purpose": "что развивает тренировка в целом",\n'
        '  "block_contrast": "чем блоки отличаются и зачем такой порядок (физиологический смысл)",\n'
        '  "target_athlete": "на кого рассчитана, к чему готовит",\n'
        '  "intensity_level": "лёгкая / средняя / тяжёлая / очень тяжёлая",\n'
        '  "what_to_watch": "на что обратить внимание, типичные ошибки",\n'
        '  "total_volume_km": 13.4,\n'
        '  "is_borderline": true,\n'
        '  "borderline_note": "на 300м быстрые группы работают на МПК, умеренные — на ПАНО (или null)",\n'
        '  "groups": [\n'
        "    {\n"
        '      "number": "1",\n'
        '      "from_comment": false,\n'
        '      "track_note": null,\n'
        '      "reps_override": null,\n'
        '      "health_group": false,\n'
        '      "blocks": [\n'
        '        {"block": 1, "work_pace_start": "3:20", "work_pace_end": "3:05", "work_distance_m": 300, "recovery_pace": "5:00", "active_recovery": true},\n'
        '        {"block": 3, "work_pace_start": "2:40", "work_pace_end": "2:40", "work_distance_m": 100, "recovery_pace": "5:00", "active_recovery": true}\n'
        "      ]\n"
        "    }\n"
        "  ],\n"
        '  "extra_groups": [\n'
        '    {"number": "3.5", "description": "работает с группой 3", "source": "комментарий"}\n'
        "  ],\n"
        '  "has_progression": false,\n'
        '  "even_pace_available": false,\n'
        '  "coach_notes": "уточнения для ВСЕХ (не индивидуальные)",\n'
        '  "ignored": "что проигнорировано и почему"\n'
        "}\n"
        "Для long: structure=[], overall_purpose=null, block_contrast=null, target_athlete=null,\n"
        "intensity_level=null, what_to_watch=null, total_volume_km=null, is_borderline=false,\n"
        "borderline_note=null, groups[] в старом формате с полем work и blocks=[].\n"
        "Отвечай только JSON."
    )


def analyze_workout(raw_text: str, comments_text: str = "", mode: str = "deep") -> dict | None:
    """Шаг 1 двухшаговой обработки: анализ анонса тренировки через DeepSeek.

    mode="deep"  → deepseek-v4-pro,   max_tokens=8000, timeout=300s
    mode="smart" → deepseek-v4-flash, max_tokens=8000, timeout=180s

    Возвращает структурированный dict с разбором тренировки или None при ошибке.
    """
    import re as _re

    prompt = _build_analyze_prompt(raw_text, comments_text or "(нет комментариев)")

    if mode == "deep":
        model, api_timeout = MODEL_DEEP, 300
    else:
        model, api_timeout = MODEL_SMART, 180
    max_tok = 8000

    t0 = _time.time()
    raw = ""

    for attempt in range(2):
        try:
            response = _get_client().chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tok,
                temperature=0,
                timeout=api_timeout,
            )
            usage = response.usage
            in_tok = usage.prompt_tokens if usage else None
            out_tok = usage.completion_tokens if usage else None

            msg = response.choices[0].message
            reasoning = getattr(msg, "reasoning_content", None)
            raw = (msg.content or "").strip()

            # Fallback: пустой content → ищем JSON в reasoning_content
            if not raw and reasoning:
                m = _re.search(r'\{[\s\S]*\}', reasoning)
                if m:
                    raw = m.group(0)
                    logger.info("analyze_workout: JSON извлечён из reasoning_content")

            if not raw:
                if attempt == 0:
                    logger.warning("analyze_workout: пустой ответ (попытка 1), повтор через 3 сек")
                    _time.sleep(3)
                    continue
                logger.error("analyze_workout: пустой ответ (попытка 2)")
                return None
            break

        except Exception as e:
            err_type = type(e).__name__
            if "Timeout" in err_type or "timeout" in str(e).lower():
                logger.error(f"analyze_workout: timeout после {round(_time.time() - t0, 1)}s (limit={api_timeout}s)")
                return None
            if attempt == 0:
                logger.warning(f"analyze_workout: ошибка API (попытка 1): {e}, повтор через 3 сек")
                _time.sleep(3)
                continue
            logger.error(f"analyze_workout: ошибка API: {e}")
            return None

    elapsed = round(_time.time() - t0, 1)

    # ── Парсинг JSON ──────────────────────────────────────────
    raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"analyze_workout: полный JSON невалиден ({e}), пробую вырезать {{...}}")
        m = _re.search(r'\{[\s\S]*\}', raw)
        if m:
            try:
                parsed = json.loads(m.group(0))
                logger.info("analyze_workout: JSON извлечён regex-ом")
            except json.JSONDecodeError as e2:
                logger.error(f"analyze_workout: regex JSON тоже невалиден: {e2} | raw[:300]={raw[:300]!r}")
                return None
        else:
            logger.error(f"analyze_workout: JSON не найден | raw[:300]={raw[:300]!r}")
            return None

    if not isinstance(parsed, dict):
        logger.error(f"analyze_workout: результат не dict: {type(parsed).__name__}")
        return None

    parsed["_stats"] = {
        "time_sec": elapsed,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "mode": mode,
        "model": model,
    }

    logger.info(
        f"analyze_workout: valid={parsed.get('is_valid')}, "
        f"type={parsed.get('workout_type')}, "
        f"groups={len(parsed.get('groups') or [])}, "
        f"extra={len(parsed.get('extra_groups') or [])}, "
        f"mode={mode}, time={elapsed}s, "
        f"tokens={in_tok}/{out_tok}"
    )
    return parsed


# ══════════════════════════════════════════════════════════════
#  ШАГ 2 — Рекомендация группы (на готовом анализе + данных юзера)
# ══════════════════════════════════════════════════════════════

# Человекочитаемые названия зон
_ZONE_LABELS = {
    "easy":       "лёгкая (E)",
    "marathon":   "темповая (M)",
    "threshold":  "ПАНО (T)",
    "interval":   "МПК (I)",
    "repetition": "скоростная (R)",
}

# Относительная интенсивность зоны (для влияния восстановления)
_ZONE_INTENSITY = {
    "easy": 0.20, "marathon": 0.50, "threshold": 0.70,
    "interval": 0.90, "repetition": 1.0,
}

# Ценность зоны для каждой специализации (0..1)
_SPEC_ZONE_VALUE = {
    "5k":            {"repetition": 0.70, "interval": 1.00, "threshold": 0.80, "marathon": 0.50, "easy": 0.30},
    "10k":           {"repetition": 0.60, "interval": 1.00, "threshold": 0.85, "marathon": 0.60, "easy": 0.30},
    "half_marathon": {"repetition": 0.40, "interval": 0.70, "threshold": 1.00, "marathon": 0.90, "easy": 0.50},
    "marathon":      {"repetition": 0.30, "interval": 0.60, "threshold": 0.95, "marathon": 1.00, "easy": 0.60},
    "speed":         {"repetition": 1.00, "interval": 0.90, "threshold": 0.50, "marathon": 0.30, "easy": 0.20},
    "fitness":       {"repetition": 0.40, "interval": 0.60, "threshold": 0.70, "marathon": 0.80, "easy": 0.80},
}

_SPEC_LABELS = {
    "5k": "5 км", "10k": "10 км", "half_marathon": "полумарафон",
    "marathon": "марафон", "speed": "развитие скорости", "fitness": "общая форма",
}

# ── ТЮНИНГ: % подходимости ────────────────────────────────────
# borderline: % = полезность_зоны × quality_volume.
# quality_volume = intensity × time_to_termination — накопленный объём качественной
# работы за тренировку (интеграл интенсивности по времени до отказа). Имеет максимум
# у порога: слишком медленно → интенсивность≈0; слишком быстро → время до отказа≈0.
_USEFULNESS_FLOOR = 0.72     # полезность зоны — тилт 0.72..1.0 (характер×специализация)
_INTENSITY_EXP = 4.0         # крутизна роста интенсивности с темпом (отн. скорости к порогу)
_TTT_TAU = 28.0              # сек/км превышения порога, где время до отказа ≈ половина
_TTT_STEEP = 5.5             # крутизна НЕЛИНЕЙНОГО обрыва времени до отказа выше порога
_TTT_RECOVERY_K = 0.35       # усталость приближает грань отказа (rec 0..1)
# non-borderline (группы только «быстрее/медленнее»): спец не влияет, % по чистой форме
_W_FORM_NB, _W_REC_NB = 0.58, 0.42

# Зоны от быстрой к медленной + короткие имена
_ZONE_ORDER = ["repetition", "interval", "threshold", "marathon", "easy"]
_ZONE_NAME = {  # без буквенного индекса — для разметки положения
    "repetition": "скоростная", "interval": "МПК", "threshold": "ПАНО",
    "marathon": "темповая", "easy": "лёгкая",
}
_ZONE_NEAR = {  # форма «близко к …»
    "repetition": "скоростной", "interval": "МПК", "threshold": "ПАНО",
    "marathon": "темповой", "easy": "лёгкой",
}

# Что РЕАЛЬНО развивает тренировка данного характера (character→zone affinity 0..1).
# speed — короткие быстрые отрезки; vo2 — средние интервалы; tempo — длинные/пороговые.
_CHARACTER_ZONE = {
    "speed": {"repetition": 1.00, "interval": 0.90, "threshold": 0.50, "marathon": 0.25, "easy": 0.10},
    "vo2":   {"repetition": 0.70, "interval": 1.00, "threshold": 0.80, "marathon": 0.40, "easy": 0.15},
    "tempo": {"repetition": 0.30, "interval": 0.60, "threshold": 1.00, "marathon": 0.90, "easy": 0.40},
}


def _workout_character(analysis: dict) -> str:
    """Характер тренировки: 'speed' / 'vo2' / 'tempo' — по длине отрезков и purpose."""
    dists = [b.get("work_distance_m") or 0
             for b in (analysis.get("structure") or []) if b.get("type") == "repeat"]
    dom = max(dists) if dists else 0
    text = " ".join(
        [str(analysis.get("overall_purpose") or "")]
        + [str(b.get("purpose") or "") for b in (analysis.get("structure") or [])]
    ).lower()
    speed_kw = any(k in text for k in ("скорост", "спринт", "нейромыш", "максимал"))
    tempo_kw = any(k in text for k in ("пано", "темпов", "порог", "марафон"))

    if dom and dom <= 400:
        char = "speed"
    elif dom and dom <= 1000:
        char = "vo2"
    elif dom > 1000:
        char = "tempo"
    else:
        char = "vo2"
    # мягкая корректировка по ключевым словам
    if char == "vo2" and tempo_kw and not speed_kw:
        char = "tempo"
    if char in ("vo2", "tempo") and speed_kw and not tempo_kw and dom and dom <= 500:
        char = "speed"
    return char


def _spec_component(zone: str, spec: str, character: str) -> float:
    """Ценность зоны = что тренировка СПОСОБНА дать (характер) × приоритет специализации.
    Характер доминирует (зона, которую тренировка не тренирует, ценится низко даже под цель);
    специализация лишь тилтит приоритет среди того, что тренировка даёт.
    """
    char_v = _CHARACTER_ZONE.get(character, _CHARACTER_ZONE["vo2"]).get(zone, 0.5)
    base = _SPEC_ZONE_VALUE.get(spec, _SPEC_ZONE_VALUE["half_marathon"]).get(zone, 0.5)
    return char_v * (0.6 + 0.4 * base)


def _zone_position(pace_s: float, zone_key: str, zone_secs: dict) -> tuple[str, str | None]:
    """Положение темпа внутри зоны: (верх/середина/низ зоны, 'близко к …' | None).
    Границы зоны — середины до соседних зон пользователя. Верх = быстрый край.
    """
    order = sorted([z for z in zone_secs if zone_secs.get(z) is not None],
                   key=lambda z: zone_secs[z])
    if zone_key not in order:
        return "", None
    idx = order.index(zone_key)
    center = zone_secs[zone_key]
    faster = order[idx - 1] if idx > 0 else None          # меньше сек = быстрее
    slower = order[idx + 1] if idx + 1 < len(order) else None
    lower_edge = (center + zone_secs[faster]) / 2 if faster else center - 20
    upper_edge = (center + zone_secs[slower]) / 2 if slower else center + 20
    span = upper_edge - lower_edge
    if span <= 0:
        return "середина зоны", None
    frac = max(0.0, min(1.0, (pace_s - lower_edge) / span))  # 0=быстрый край, 1=медленный
    if frac < 0.33:
        pos = "верх зоны"
    elif frac > 0.66:
        pos = "низ зоны"
    else:
        pos = "середина зоны"
    near = None
    if frac <= 0.18 and faster:
        near = f"близко к {_ZONE_NEAR.get(faster, faster)}"
    elif frac >= 0.82 and slower:
        near = f"близко к {_ZONE_NEAR.get(slower, slower)}"
    return pos, near


def _zone_display(zone_key: str, pos: str, near: str | None) -> str:
    """'темповая (верх зоны, близко к ПАНО)'."""
    name = _ZONE_NAME.get(zone_key, zone_key)
    inner = ", ".join(x for x in (pos, near) if x)
    return f"{name} ({inner})" if inner else name


def build_ai_b_prompt(analysis: dict, user_data: dict, zones_map: dict, recovery: dict | None) -> str:
    """Промпт варианта B: ИИ видит Хшаг 1 + данные бегуна и САМ выбирает группу.
    Без формул — чистая рекомендация от ИИ.
    """
    groups = analysis.get("groups") or []
    structure = analysis.get("structure") or []
    spec = user_data.get("specialization") or "half_marathon"
    spec_labels = {
        "5k": "5 км", "10k": "10 км", "half_marathon": "полумарафон",
        "marathon": "марафон", "speed": "развитие скорости", "fitness": "общая форма",
    }
    spec_label = spec_labels.get(spec, spec)

    # Зоны
    zone_lines = []
    for z, p in (zones_map or {}).items():
        zone_lines.append(f"  {z}: {p} мин/км")
    zones_text = "\n".join(zone_lines) if zone_lines else "  нет данных"

    # Восстановление
    rec_parts = []
    if recovery:
        rv = recovery.get("recovery_score")
        if rv is not None:
            rec_parts.append(f"Recovery Score: {rv}")
        tr = recovery.get("training_readiness")
        if isinstance(tr, dict) and tr.get("score") is not None:
            rec_parts.append(f"Training Readiness: {tr['score']}/100")
        hrv = recovery.get("hrv")
        if hrv is not None:
            rec_parts.append(f"HRV: {hrv} мс")
        bb = recovery.get("body_battery")
        if bb is not None:
            rec_parts.append(f"Body Battery: {bb}")
    rec_text = ", ".join(rec_parts) if rec_parts else "нет данных"

    # Структура тренировки
    struct_lines = []
    for b in structure:
        if b.get("type") == "easy":
            struct_lines.append(f"  {b.get('distance_m')} м — лёгкий бег")
        else:
            struct_lines.append(
                f"  Блок {b.get('block')}: {b.get('reps')} × {b.get('work_distance_m')} м / "
                f"{b.get('recovery_distance_m')} м восст — {b.get('purpose', '')}"
            )
    struct_text = "\n".join(struct_lines) if struct_lines else "  —"

    # Группы
    groups_lines = []
    for g in groups:
        if g.get("health_group"):
            groups_lines.append(f"  Группа {g.get('number')}: бег/ходьба (группа здоровья)")
            continue
        blocks = g.get("blocks") or []
        block_strs = []
        for bl in blocks:
            ps = bl.get("work_pace_start") or "?"
            pe = bl.get("work_pace_end")
            pace_str = f"{ps}→{pe}" if pe and pe != ps else ps
            rp = bl.get("recovery_pace") or "—"
            block_strs.append(f"бл{bl.get('block')} {pace_str}/км (recovery {rp})")
        groups_lines.append(f"  Группа {g.get('number')}: {'; '.join(block_strs)}")
    groups_text = "\n".join(groups_lines) if groups_lines else "  —"

    json_schema = (
        "{"
        + '"recommended_group": "номер группы (например: 3)", '
        + '"recommended_pace": "темп основной группы (например: 4:00–4:25 мин/км)", '
        + '"reason": "1-2 предложения: почему эта группа — укажи зоны и восстановление", '
        + '"if_feeling_good": "что делать если ноги бегут легко на разминке — группа выше с темпом", '
        + '"if_tired": "что делать если тяжело — группа ниже или промежуточный темп X.5 с цифрами", '
        + '"gap_note": "вывод по разрывам: небольшой (можно переходить) или большой (нужна промежуточная)", '
        + '"suitability_percentages": [{"group": "номер", "percentage": 0..100, "comment": "1-2 слова"}], '
        + '"preparation_tips": ["совет 1"], '
        + '"warning": "предупреждение или null"'
        + "}"
    )
    return (
        "Ты — беговой тренер клуба Dusty Dumbbells.\n"
        "Подбери группу для бегуна. Отвечай тренерски — без шаблонов, живым языком.\n\n"
        f"ТРЕНИРОВКА: {analysis.get('workout_date', '—')}\n"
        f"Суть: {analysis.get('summary', '—')}\n"
        f"Цель: {analysis.get('overall_purpose', '—')}\n"
        f"На что обратить внимание: {analysis.get('what_to_watch', '—')}\n\n"
        "СТРУКТУРА (одинаково для всех):\n"
        f"{struct_text}\n\n"
        "ГРУППЫ (отличаются только темпами):\n"
        f"{groups_text}\n\n"
        "ДАННЫЕ БЕГУНА:\n"
        "Зоны темпа:\n"
        f"{zones_text}\n"
        f"Специализация: {spec_label}\n"
        f"Восстановление: {rec_text}\n\n"
        "Дай рекомендацию строго в формате JSON:\n\n"
        f"{json_schema}\n\n"
        "Рассмотри альтернативы: какая группа для скорости, какая для восстановления.\n"
        "Группа с максимальным percentage ДОЛЖНА совпадать с recommended_group.\n"
        "Поле 'group' в suitability_percentages — ТОЛЬКО номер (допустимо: '1','2','3','3.5','4','5').\n"
        "ЗАПРЕЩЕНО: 'Группа 3', '3 быстрая'. Только цифра или цифра с точкой.\n"
        "Отвечай только JSON, без лишнего текста."
    )


def generate_ai_b_recommendation(analysis: dict, user_data: dict, zones_map: dict,
                                  recovery: dict | None, mode: str = "smart") -> tuple[str, dict]:
    """Чистая ИИ-рекомендация (вариант B). Возвращает (text, stats)."""
    prompt = build_ai_b_prompt(analysis, user_data, zones_map, recovery)
    if mode == "deep":
        model, max_tok, timeout = MODEL_DEEP, 4000, 180
    else:
        model, max_tok, timeout = MODEL_SMART, 2000, 60

    t0 = _time.time()
    stats = {"time_sec": 0, "mode": mode, "input_tokens": None, "output_tokens": None}
    try:
        resp = _get_client().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tok,
            temperature=0.4,
            timeout=timeout,
        )
        usage = resp.usage
        stats = {
            "time_sec": round(_time.time() - t0, 1),
            "mode": mode,
            "input_tokens": usage.prompt_tokens if usage else None,
            "output_tokens": usage.completion_tokens if usage else None,
        }
        raw = (resp.choices[0].message.content or "").strip()
        # Парсим JSON ответ
        import re as _re2
        raw_clean = _re2.sub(r"<think>.*?</think>", "", raw, flags=_re2.DOTALL).strip()
        raw_clean = raw_clean.replace("```json", "").replace("```", "").strip()
        advice: dict = {}
        try:
            advice = json.loads(raw_clean)
        except Exception:
            m2 = _re2.search(r'\{[\s\S]*\}', raw_clean)
            if m2:
                try:
                    advice = json.loads(m2.group(0))
                except Exception:
                    advice = {}
        if not isinstance(advice, dict):
            advice = {}
        # Поля из анализа (Шаг 1) — ИИ их не дублирует, подставляем в коде
        advice["overall_purpose"] = analysis.get("overall_purpose", "")
        advice["workout_summary"] = analysis.get("summary", "")
        spec = user_data.get("specialization") or "half_marathon"
        advice["spec_label"] = _SPEC_LABELS.get(spec, spec)
        # Санитайз номеров групп в suitability_percentages
        for item in (advice.get("suitability_percentages") or []):
            if "group" in item:
                item["group"] = _sanitize_group_name(str(item["group"]))
        return advice, stats
    except Exception as e:
        logger.warning(f"generate_ai_b_recommendation error: {e}")
        stats["time_sec"] = round(_time.time() - t0, 1)
        return {}, stats


def format_ai_b_message(text: str, analysis: dict, stats: dict) -> str:
    """Форматирует сообщение варианта B для админа."""
    import html as _h
    date = analysis.get("workout_date", "—")
    t = stats.get("time_sec", "?")
    inp = stats.get("input_tokens", "?")
    out = stats.get("output_tokens", "?")
    mode = stats.get("mode", "?")
    body = text if text else "❌ Модель не ответила."
    return (
        f"🧪 <b>Вариант B — чистый ИИ ({date})</b>\n"
        f"Без формул, ИИ сам выбирает группу.\n\n"
        f"{body}\n\n"
        f"<i>⏱ {t}с | {mode} | 📥 {inp} / 📤 {out}</i>"
    )


def _pace_sec(pace: str) -> float | None:
    """'3:53' / '3.53' → 233 сек."""
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


def _classify_zone(pace_s: float, zone_secs: dict) -> str | None:
    """Ближайшая зона пользователя для заданного темпа."""
    best, best_d = None, None
    for z, s in zone_secs.items():
        if s is None:
            continue
        d = abs(pace_s - s)
        if best_d is None or d < best_d:
            best_d, best = d, z
    return best


def _form_score(pace_s: float, zone_secs: dict) -> float:
    """Фит к рабочей зоне атлета (близость к целевой рабочей зоне).
    1.0 если темп в диапазоне R..T (посильная интенсивная работа),
    падает если быстрее R (непосильно) или медленнее T (для интервальной — слишком легко).
    """
    rep = zone_secs.get("repetition")
    thr = zone_secs.get("threshold")
    if rep is None or thr is None:
        return 1.0
    if pace_s < rep:                       # быстрее повторной — слишком тяжело
        return max(0.0, 1.0 - (rep - pace_s) / 30.0)
    if pace_s <= thr:                      # в рабочем диапазоне R..T — идеально по силам
        return 1.0
    return max(0.0, 1.0 - (pace_s - thr) / 60.0)  # медленнее порога — ценность падает


def _recovery_value(recovery: dict | None) -> float | None:
    """Достаёт 0..100 из recovery (Recovery Score / Training Readiness)."""
    if not recovery:
        return None
    for k in ("recovery_score", "training_readiness", "readiness", "score", "recovery"):
        v = recovery.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            s = v.get("score")
            if isinstance(s, (int, float)):
                return float(s)
    return None


def _group_primary_pace(group: dict, struct_dist: dict) -> str | None:
    """Усреднённый темп определяющего (самого длинного) рабочего блока группы.
    Если блок с прогрессией (start≠end) — возвращает среднее, округляя до целых секунд.
    """
    blocks = group.get("blocks") or []
    if not blocks:
        return None
    best_block = max(
        blocks,
        key=lambda b: struct_dist.get(b.get("block"), 0) or 0,
    )
    # Новый формат: work_pace_start / work_pace_end
    start_pace = best_block.get("work_pace_start")
    end_pace = best_block.get("work_pace_end")
    if start_pace and end_pace:
        start_sec = _pace_sec(start_pace)
        end_sec = _pace_sec(end_pace)
        if start_sec is not None and end_sec is not None:
            avg_sec = round((start_sec + end_sec) / 2)
            m, s = divmod(avg_sec, 60)
            return f"{m}:{s:02d}"
    # Fallback: старый формат (редко, только при миграции)
    return best_block.get("work_pace")


def _intensity(pace_s: float, thr_sec: float | None) -> float:
    """Относительная интенсивность темпа группы (отн. скорости к порогу), растёт с темпом.
    1.0 на пороге, >1 быстрее, <1 медленнее (резко падает на медленных — «качества» нет).
    """
    if not thr_sec or pace_s <= 0:
        return 1.0
    ratio = thr_sec / pace_s
    if ratio <= 1.0:
        return ratio ** _INTENSITY_EXP        # ниже/на пороге — степенной
    else:
        return 1.0 + (ratio - 1.0) ** 0.5    # выше порога — корневой (медленнее)


def _time_to_termination(pace_s: float, thr_sec: float | None, rec: float) -> float:
    """Доля времени тренировки, которую бегун реально удержит этот темп (0..1).
    Ниже порога — плато (лактат в равновесии, держится долго). Выше порога — РЕЗКИЙ
    нелинейный обрыв (накопление лактата → отказ). Усталость приближает грань.
    """
    if thr_sec is None:
        return 1.0
    delta = thr_sec - pace_s           # >0 = быстрее порога
    if delta <= 0:
        return 1.0                     # у порога или медленнее — удержит всю тренировку
    eff = delta * (1.0 + (1.0 - rec) * _TTT_RECOVERY_K)   # уставший → грань ближе
    return 1.0 / (1.0 + (eff / _TTT_TAU) ** _TTT_STEEP)


def _zone_usefulness(zone: str, spec: str, character: str) -> float:
    """Полезность зоны (мягкий тилт 0.8..1.0): характер тренировки × специализация.
    Завершаемость доминирует, поэтому полезность — гентл-множитель, а не главный фактор.
    """
    return _USEFULNESS_FLOOR + (1.0 - _USEFULNESS_FLOOR) * _spec_component(zone, spec, character)


def _quality_label(pace_s: float, peak_pace_s: float, ttt: float,
                   rec: float, zone_key: str) -> str | None:
    """Метка из положения на кривой quality_volume относительно оптимума (пика)."""
    if pace_s < peak_pace_s - 0.5:          # быстрее оптимума — выше по интенсивности
        if ttt < 0.25:
            return "риск схода/травмы, мало времени работы"
        return "тяжело, но реально"
    if pace_s > peak_pace_s + 0.5:          # медленнее оптимума — ниже интенсивность
        if rec < 0.45:
            return "разгрузочный вариант на сегодня"
        if zone_key == "easy":              # реально лёгкая зона
            return "слишком легко"
        return "низкий стимул"              # темповая/пороговая, но ниже оптимума
    return None                              # у максимума — оптимум работы


def recommend_group(analysis_json: dict, user_data: dict) -> dict | None:
    """Шаг 2: рекомендация группы на основе готового анализа Шага 1 + данных юзера.

    analysis_json — результат analyze_workout (interval-схема со structure/groups/is_borderline).
    user_data — {db_user_id, specialization?, recovery?}. Зоны берутся через get_pace_zones().

    Возвращает структурированный dict (markup групп, основная/альтернативы, текст) или
    {"ok": False, "note": ...} если рекомендация невозможна.
    """
    import zones as _zones

    analysis = analysis_json or {}
    if analysis.get("workout_type") != "interval" or not analysis.get("structure"):
        return {"ok": False, "note": "Рекомендация по зонам доступна для интервальных тренировок."}

    # ── Специализация (по умолчанию полумарафон) ──────────────
    spec = user_data.get("specialization") or "half_marathon"
    if spec not in _SPEC_ZONE_VALUE:
        spec = "half_marathon"
    spec_is_default = not user_data.get("specialization")

    # ── Зоны пользователя (готовые, НЕ считаем здесь) ─────────
    db_user_id = user_data.get("db_user_id")
    zinfo = _zones.get_pace_zones(db_user_id) if db_user_id is not None else None
    if not zinfo or not zinfo.get("zones"):
        return {"ok": False, "note": "Нет персональных зон — заполни профиль (VO2max или лактатный порог)."}
    zones_map = zinfo["zones"]
    zones_source = zinfo.get("source")
    zone_secs = {z: _pace_sec(p) for z, p in zones_map.items()}

    # ── Восстановление (0..1; нейтральное 0.7 если нет данных) ─
    rec_raw = _recovery_value(user_data.get("recovery"))
    rec = (rec_raw if rec_raw is not None else 70.0) / 100.0

    # ── Карта дистанций блоков из структуры ───────────────────
    struct_dist = {}
    for b in analysis.get("structure") or []:
        if b.get("type") == "repeat":
            struct_dist[b.get("block")] = b.get("work_distance_m") or 0

    is_borderline = bool(analysis.get("is_borderline"))
    character = _workout_character(analysis)
    thr_sec = zone_secs.get("threshold")

    # ── Проход 1: метрики каждой группы (порядок сохраняем в rows) ──
    rows = []         # все группы по порядку (для разметки)
    for g in analysis.get("groups") or []:
        number = g.get("number", "?")
        if g.get("health_group"):
            rows.append({"number": number, "kind": "health", "zone_disp": "бег/ходьба",
                         "work_pace": None, "pct": None, "tag": None})
            continue
        primary = _group_primary_pace(g, struct_dist)
        psec = _pace_sec(primary)
        if psec is None:
            rows.append({"number": number, "kind": "na", "zone_disp": "—",
                         "work_pace": primary, "pct": None, "tag": None})
            continue
        zone = _classify_zone(psec, zone_secs)
        pos, near = _zone_position(psec, zone, zone_secs)
        intensity = _intensity(psec, thr_sec)
        ttt = _time_to_termination(psec, thr_sec, rec)
        zint = _ZONE_INTENSITY.get(zone, 0.7)
        rows.append({
            "number": number, "kind": "paced", "zone_key": zone,
            "zone_label": _ZONE_LABELS.get(zone, zone), "zone_disp": _zone_display(zone, pos, near),
            "zone_pos": pos, "zone_near": near, "work_pace": primary, "psec": psec,
            "intensity": round(intensity, 3), "ttt": round(ttt, 3), "qv_raw": intensity * ttt,
            "use": _zone_usefulness(zone, spec, character),
            "form": _form_score(psec, zone_secs), "rec_fit": 1.0 - zint * (1.0 - rec),
            "from_comment": bool(g.get("from_comment")),
        })

    scored = [r for r in rows if r["kind"] == "paced"]
    if not scored:
        return {"ok": False, "note": "Не удалось разметить группы по зонам."}

    # ── Проход 2: нормировка quality_volume к [0..1] и % ──────
    qv_max = max(r["qv_raw"] for r in scored) or 1.0
    for r in scored:
        r["qvnorm"] = r["qv_raw"] / qv_max if qv_max else 0.0
        if is_borderline:
            r["pct"] = max(0, min(99, round(100 * r["use"] * r["qvnorm"])))
            # alt_pct: та же формула что pct, но usefulness считается под альт-специализацию.
            # qvnorm остаётся тем же — завершаемость не зависит от цели.
            best_spec, best_pct = None, r["pct"]
            for s in _SPEC_ZONE_VALUE:
                if s == spec:
                    continue
                p = max(0, min(99, round(100 * _zone_usefulness(r["zone_key"], s, character) * r["qvnorm"])))
                if p > best_pct:
                    best_pct, best_spec = p, s
            r["alt_spec"], r["alt_pct"] = best_spec, best_pct
        else:
            r["pct"] = max(0, min(99, round(100 * (_W_FORM_NB * r["form"] + _W_REC_NB * r["rec_fit"]))))
            r["alt_spec"], r["alt_pct"] = None, r["pct"]

    scored.sort(key=lambda r: r["pct"], reverse=True)
    main = scored[0]

    # ── Метки из положения на кривой относительно оптимума ────
    peak_pace = main["psec"]
    for r in scored:
        if is_borderline:
            r["tag"] = None if r is main else _quality_label(r["psec"], peak_pace, r["ttt"], rec, r["zone_key"])
        else:
            r["tag"] = None

    markup = [{"number": r["number"], "zone_disp": r["zone_disp"],
               "work_pace": r.get("work_pace"), "pct": r["pct"], "tag": r.get("tag"),
               "alt_pct": r.get("alt_pct"), "alt_spec": r.get("alt_spec")}
              for r in rows]

    # ── Альтернативы ──────────────────────────────────────────
    alternatives = []
    if is_borderline:
        for e in scored[1:]:
            cond = None
            if e["alt_spec"] and e["alt_pct"] - e["pct"] >= 12:
                cond = f"но {e['alt_pct']}% если цель — {_SPEC_LABELS[e['alt_spec']]}"
            alternatives.append({
                "number": e["number"], "pct": e["pct"], "zone_label": e["zone_label"],
                "zone_disp": e["zone_disp"], "tag": e.get("tag"), "condition": cond,
            })

    spec_label = _SPEC_LABELS.get(spec, spec)

    # ── Текст ─────────────────────────────────────────────────
    lines = []
    spec_suffix = " (по умолчанию)" if spec_is_default else ""
    lines.append(f"🎯 Специализация: {spec_label.capitalize()}{spec_suffix}")
    lines.append(f"📊 Зоны (источник: {zones_source}) · характер: {character}\n")

    lines.append("Разметка групп под тебя:")
    for m in markup:
        if m["pct"] is None:
            lines.append(f"  Группа {m['number']} — {m['zone_disp']}")
        else:
            tag = f", {m['tag']}" if m.get("tag") else ""
            lines.append(f"  Группа {m['number']} — {m['zone_disp']}, темп {m['work_pace']} ({m['pct']}%{tag})")

    if is_borderline:
        lines.append(f"\n✅ Рекомендую: Группа {main['number']} — {main['zone_disp']}, {main['pct']}%")
        if alternatives:
            lines.append("Альтернативы:")
            for a in alternatives:
                base = f"  Группа {a['number']} — {a['pct']}% для твоей цели ({spec_label})"
                if a.get("tag"):
                    base += f" — {a['tag']}"
                if a["condition"]:
                    base += f", {a['condition']}"
                lines.append(base)
    else:
        # Выбор качеств нет — группы отличаются только темпом «по силам»
        lines.append(f"\n✅ Рекомендую: Группа {main['number']} (по силам), {main['pct']}%")
        lines.append("Группы отличаются только темпом — выбери по самочувствию.")

    return {
        "ok": True,
        "workout_type": "interval",
        "specialization": spec,
        "specialization_label": spec_label,
        "specialization_is_default": spec_is_default,
        "is_borderline": is_borderline,
        "workout_character": character,
        "zones_source": zones_source,
        "groups": markup,
        "main_group": {"number": main["number"], "pct": main["pct"],
                       "zone_label": main["zone_label"], "zone_disp": main["zone_disp"]},
        "alternatives": alternatives,
        "text": "\n".join(lines),
    }


# ── Шаг 2 для ДЛИТЕЛЬНОЙ (long) — облегчённая рекомендация ─────

def _is_health_long(group: dict) -> bool:
    """Группа здоровья/ходоков в длительной."""
    if group.get("health_group"):
        return True
    w = (group.get("work") or "").lower()
    return "ходьб" in w or "чередован" in w


def _extract_long_pace(group: dict) -> str | None:
    """Достаёт представительный (первый = базовый/первая половина) темп из текста группы."""
    import re as _re
    for field in ("work", "recovery", "description"):
        txt = group.get(field)
        if not txt:
            continue
        m = _re.search(r'\b(\d{1,2}[:.]\d{2})\b', str(txt))
        if m:
            return m.group(1).replace(".", ":")
    return None


def recommend_long(analysis_json: dict, user_data: dict) -> dict | None:
    """Облегчённая рекомендация для длительной (100 минут).

    Длительная — доп. сервис: без трёхкомпонентного %, акцент на «потянешь ли
    100 минут комфортно». Зоны берутся готовыми (get_pace_zones). Специализация
    почти не влияет (длительная = аэробная база под полумарафон/марафон).

    Возвращает структуру, совместимую с отображением /workout (main_group +
    alternatives + заметки), или {"ok": False, "note": ...}.
    """
    import zones as _zones

    analysis = analysis_json or {}
    if analysis.get("workout_type") != "long":
        return {"ok": False, "note": "Это не длительная тренировка."}

    has_progression = bool(analysis.get("has_progression"))
    groups = analysis.get("groups") or []

    # Разделяем обычные группы и группу здоровья
    health = next((g for g in groups if _is_health_long(g)), None)
    paced = []
    for g in groups:
        if _is_health_long(g):
            continue
        pace = _extract_long_pace(g)
        paced.append({"number": g.get("number", "?"), "pace": pace,
                      "pace_sec": _pace_sec(pace)})

    progression_note = (
        "💡 Прогрессия: вторую половину можно на ~30 сек/км быстрее (опция)."
        if has_progression else
        "💡 Прогрессия: вторая половина быстрее — по желанию (~30 сек/км)."
    )
    even_note = "Ровный темп — норма и доступен всегда."

    # ── Зоны пользователя ─────────────────────────────────────
    db_user_id = user_data.get("db_user_id")
    zinfo = _zones.get_pace_zones(db_user_id) if db_user_id is not None else None
    zones_map = (zinfo or {}).get("zones") if zinfo else None
    zones_source = (zinfo or {}).get("source") if zinfo else None

    # ── Профиль пустой / нет зон → консервативный ориентир ────
    if not zones_map or not any(p["pace_sec"] for p in paced):
        lines = ["🕐 Длительная — 100 минут\n"]
        lines.append("Профиль не заполнен — точный темп не подскажу. Ориентир:")
        if paced:
            mid = paced[len(paced) // 2]
            lines.append(f"  • Бегаешь регулярно → средний по темпу вариант (Группа {mid['number']}),")
            lines.append("    главное — держать ровно и комфортно все 100 минут.")
        else:
            lines.append("  • Бегаешь регулярно → выбери средний по темпу вариант, держи ровно 100 минут.")
        if health:
            lines.append(f"  • Новичкам → Группа {health.get('number', 'здоровья')} (бег/ходьба) — это нормально и полезно.")
        else:
            lines.append("  • Новичкам → начни с комфортного бег/ходьба, без гонки за темпом.")
        lines.append(f"\n{progression_note}\n{even_note}")
        return {
            "ok": True, "workout_type": "long", "profile_empty": True,
            "zones_source": zones_source,
            "main_group": ({"number": paced[len(paced) // 2]["number"]} if paced else None),
            "alternatives": [], "health_group": (health or {}).get("number") if health else None,
            "progression_note": progression_note, "even_pace_note": even_note,
            "text": "\n".join(lines),
        }

    # ── Подбор по форме ──────────────────────────────────────
    zone_secs = {z: _pace_sec(p) for z, p in zones_map.items()}
    easy_s = zone_secs.get("easy")
    mar_s = zone_secs.get("marathon")
    # Комфортная база на 100 мин ≈ между марафонским и лёгким темпом
    if easy_s and mar_s:
        target = (easy_s + mar_s) / 2
    else:
        target = easy_s or mar_s

    with_pace = [p for p in paced if p["pace_sec"]]
    with_pace.sort(key=lambda p: p["pace_sec"])  # быстрые → медленные

    # базовая = ближайшая к комфортному ориентиру
    base_idx = min(range(len(with_pace)),
                   key=lambda i: abs(with_pace[i]["pace_sec"] - target))
    base = with_pace[base_idx]
    step_up = with_pace[base_idx - 1] if base_idx > 0 else None          # быстрее
    step_down = with_pace[base_idx + 1] if base_idx + 1 < len(with_pace) else None  # медленнее

    # Шкала подходимости (детерминированная): база = оптимум, дальше — по дистанции от неё.
    # Для длительной «слишком быстро» хуже «слишком медленно».
    groups_markup = []
    for j, p in enumerate(with_pace):
        d = j - base_idx
        if d == 0:
            pct, comment = 92, "идеально"
        elif d < 0:
            pct, comment = max(5, 92 - 22 * abs(d)), ("быстровато" if d == -1 else "слишком быстро")
        else:
            pct, comment = max(5, 92 - 14 * d), ("спокойнее" if d == 1 else "слишком медленно")
        groups_markup.append({"number": p["number"], "pace": p["pace"], "pct": pct, "comment": comment})
    if health:
        groups_markup.append({"number": health.get("number", "здоровье"), "pace": None,
                              "pct": None, "comment": "бег/ходьба"})

    def _zlabel(psec):
        return _ZONE_LABELS.get(_classify_zone(psec, zone_secs), "")

    # ── Текст ─────────────────────────────────────────────────
    lines = ["🕐 Длительная — 100 минут\n"]
    lines.append(f"🎯 Тебе по силам: Группа {base['number']} — темп ~{base['pace']}/км")
    lines.append("   Комфортная аэробная база, держишь 100 минут ровно.")

    alternatives = []
    extra = []
    if step_up:
        extra.append(f"   ⬆️ Группа {step_up['number']} (~{step_up['pace']}/км) — потемповее, "
                     f"die-hard длительная для бодрых ног")
        alternatives.append({"number": step_up["number"], "pace": step_up["pace"],
                             "zone_label": _zlabel(step_up["pace_sec"]), "role": "step_up",
                             "note": "потемповее, для бодрых ног"})
    if step_down:
        extra.append(f"   ⬇️ Группа {step_down['number']} (~{step_down['pace']}/км) — спокойнее, "
                     f"для восстановления")
        alternatives.append({"number": step_down["number"], "pace": step_down["pace"],
                             "zone_label": _zlabel(step_down["pace_sec"]), "role": "step_down",
                             "note": "спокойнее, для восстановления"})
    if health:
        extra.append(f"   (или Группа {health.get('number', 'здоровья')} — бег/ходьба, для начинающих)")
        alternatives.append({"number": health.get("number", "здоровье"), "pace": None,
                             "zone_label": "бег/ходьба", "role": "health",
                             "note": "для начинающих / восстановления"})
    if extra:
        lines.append("\nЕсли хочется иначе:")
        lines.extend(extra)

    lines.append(f"\n{progression_note}\n{even_note}")

    return {
        "ok": True,
        "workout_type": "long",
        "profile_empty": False,
        "zones_source": zones_source,
        "has_progression": has_progression,
        "groups": groups_markup,
        "main_group": {"number": base["number"], "pace": base["pace"],
                       "zone_label": _zlabel(base["pace_sec"])},
        "alternatives": alternatives,
        "health_group": (health or {}).get("number") if health else None,
        "progression_note": progression_note,
        "even_pace_note": even_note,
        "text": "\n".join(lines),
    }


def ask_groq(prompt: str, mode: str = "smart") -> dict | None:
    """Возвращает {"advice": {...}, "stats": {...}, "garmin_workout": ...} или None.
    При timeout возвращает {"advice": None, "timeout": True, "stats": {...}}.

    mode="deep"  → deepseek-v4-pro,   max_tokens=16000, timeout=300s  (~2-3 мин)
    mode="smart" → deepseek-v4-flash, max_tokens=12000, timeout=180s  (~1-2 мин)
    mode="fast"  → deepseek-v4-flash, max_tokens=8000,  timeout=60s   (~30 сек)
    """
    global last_prompt
    last_prompt = prompt
    print(f"=== PROMPT ===\n{prompt}\n=== END PROMPT ===")
    import re as _re

    if mode == "deep":
        model, max_tok, api_timeout = MODEL_DEEP, 16000, 300
    elif mode == "smart":
        model, max_tok, api_timeout = MODEL_SMART, 12000, 180
    else:
        model, max_tok, api_timeout = MODEL_FAST, 8000, 60

    t0 = _time.time()
    raw = ""
    stats = {"time_sec": 0, "input_tokens": None, "output_tokens": None, "mode": mode}

    for attempt in range(2):
        try:
            create_kwargs = dict(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tok,
                temperature=0,
                timeout=api_timeout,
            )
            response = _get_client().chat.completions.create(**create_kwargs)
            elapsed = round(_time.time() - t0, 1)
            usage = response.usage
            stats = {
                "time_sec": elapsed,
                "input_tokens": usage.prompt_tokens if usage else None,
                "output_tokens": usage.completion_tokens if usage else None,
                "mode": mode,
            }
            print(f"Stats attempt {attempt + 1}: {stats}")

            msg = response.choices[0].message
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning:
                reasoning_tokens = getattr(usage, "completion_tokens_details", None)
                effort_info = ""
                if reasoning_tokens:
                    rt = getattr(reasoning_tokens, "reasoning_tokens", None)
                    effort_info = f", reasoning_tokens={rt}" if rt else ""
                reasoning_effort = (getattr(response, "reasoning_effort", None)
                                    or getattr(usage, "reasoning_effort", None))
                if reasoning_effort:
                    effort_info += f", reasoning_effort={reasoning_effort}"
                print(f"=== THINKING ({len(reasoning)} chars{effort_info}) ===\n{reasoning[:800]}\n=== END THINKING ===")

            raw = (msg.content or "").strip()
            print(f"=== CONTENT attempt {attempt + 1} ({len(raw)} chars) ===\n{raw[:300]}\n===")

            if not raw and reasoning:
                m = _re.search(r'\{[\s\S]*\}', reasoning)
                if m:
                    raw = m.group(0)
                    print("Fallback: JSON извлечён из reasoning_content")

            if not raw:
                if attempt == 0:
                    print("Пустой ответ на попытке 1, повтор через 3 сек...")
                    _time.sleep(3)
                    continue
                print("Пустой ответ на попытке 2")
                return None

            break  # получили непустой ответ — выходим из retry-цикла

        except Exception as e:
            # Проверяем timeout через имя класса (совместимо с разными версиями openai SDK)
            err_type = type(e).__name__
            if "Timeout" in err_type or "timeout" in str(e).lower():
                elapsed = round(_time.time() - t0, 1)
                print(f"DeepSeek API timeout after {elapsed}s (limit={api_timeout}s): {e}")
                return {
                    "advice": None,
                    "stats": {"time_sec": elapsed, "mode": mode},
                    "timeout": True,
                }
            if attempt == 0:
                print(f"Ошибка DeepSeek API (попытка 1): {e}, повтор через 3 сек...")
                _time.sleep(3)
                continue
            print(f"Ошибка DeepSeek API: {e}")
            return None

    # ── Парсинг JSON ──────────────────────────────────────────
    raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    # Попытка 1: парсим весь JSON целиком
    try:
        parsed = json.loads(raw)
        garmin_workout = parsed.pop('garmin_workout', None)
        return {"advice": parsed, "stats": stats, "garmin_workout": garmin_workout}
    except json.JSONDecodeError as e:
        print(f"Полный JSON невалиден: {e} | raw[:200]={raw[:200]!r}")

    # Попытка 2: обрезаем на позиции "garmin_workout" — основная причина truncation
    # (модель обрывает JSON прямо в reason/suitability ДО garmin_workout — это не поможет)
    # Ищем ключ garmin_workout и обрезаем там
    gw_match = _re.search(r',?\s*"garmin_workout"\s*:', raw)
    if gw_match:
        raw_cut = raw[:gw_match.start()].rstrip().rstrip(',')
        if not raw_cut.endswith('}'):
            raw_cut += '}'
        try:
            parsed = json.loads(raw_cut)
            parsed.pop('garmin_workout', None)
            print("Fallback cut: advice-поля извлечены, garmin_workout обрезан")
            return {"advice": parsed, "stats": stats, "garmin_workout": None}
        except json.JSONDecodeError as e2:
            print(f"Cut fallback невалиден: {e2} | raw_cut[:200]={raw_cut[:200]!r}")

    # Попытка 3: старый regex-подход — стрип garmin_workout с конца
    raw_no_garmin = _re.sub(
        r',?\s*"garmin_workout"\s*:\s*\{[\s\S]*?\}\s*(?=\}?\s*$)',
        '',
        raw,
        flags=_re.DOTALL,
    )
    raw_no_garmin = raw_no_garmin.strip().rstrip(',').rstrip()
    if not raw_no_garmin.endswith('}'):
        raw_no_garmin += '}'
    try:
        parsed = json.loads(raw_no_garmin)
        parsed.pop('garmin_workout', None)
        print("Fallback regex-strip: advice-поля извлечены (garmin_workout вырезан regex)")
        return {"advice": parsed, "stats": stats, "garmin_workout": None}
    except json.JSONDecodeError as e3:
        print(f"Regex fallback тоже невалиден: {e3} | raw_no_garmin[:200]={raw_no_garmin[:200]!r}")

    # Попытка 4: извлекаем ключевые поля regex'ом по одному (JSON обрезан в advice-полях)
    print("Попытка 4: индивидуальное извлечение полей...")
    group_m = _re.search(r'"recommended_group"\s*:\s*"([^"]*)"', raw)
    pace_m  = _re.search(r'"recommended_pace"\s*:\s*"([^"]*)"', raw)
    if group_m and pace_m:
        minimal = {
            "recommended_group": group_m.group(1),
            "recommended_pace": pace_m.group(1),
            "reason": "—",
            "if_feeling_good": "—",
            "if_tired": "—",
            "gap_note": "",
            "suitability_percentages": [],
            "preparation_tips": [],
            "warning": None,
        }
        print(f"Fallback minimal: group={minimal['recommended_group']}, pace={minimal['recommended_pace']}")
        return {"advice": minimal, "stats": stats, "garmin_workout": None}

    print("Все попытки парсинга JSON не удались")
    return None


def ask_deepseek_garmin(workout: dict, group: str, pace: str) -> dict | None:
    """Просим DeepSeek сгенерировать JSON тренировки для Garmin Connect.

    Возвращает готовый dict (workoutName, sportType, workoutSegments, ...)
    или None при ошибке. Использует быструю модель — задача детерминированная.
    """
    import re as _re
    from datetime import datetime as _dt

    date = workout.get('workout_date', _dt.now().strftime('%Y-%m-%d'))
    date_compact = date.replace('-', '')
    work_text  = workout.get('work_text', '')
    groups_raw = workout.get('groups_raw', '')
    for extra in (workout.get('extra_groups_raw') or []):
        groups_raw += '\n' + extra

    prompt = f"""Сгенерируй JSON тренировки для загрузки в Garmin Connect API.

ТРЕНИРОВКА: {work_text}
ДАТА: {date}
РЕКОМЕНДУЕМАЯ ГРУППА: {group}
РЕКОМЕНДУЕМЫЙ ТЕМП: {pace}

ОПИСАНИЕ ВСЕХ ГРУПП:
{groups_raw}

ТРЕБОВАНИЯ К JSON:
- workoutName: "DD_{date_compact}-{group}_lvl"
- sportType: {{"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}}
- Скорость в м/с: для темпа M:SS мин/км → speed = 1000 / (M*60+SS)
  Пример: 4:20/км → 1000/260 = 3.8461538 м/с
  Если темп задан как время на отрезок (напр. 2:10 на 500 м) → speed = 500 / (2*60+10) = 500/130 = 3.8461538 м/с
- targetValueOne = БЫСТРЕЕ (большее значение м/с)
- targetValueTwo = МЕДЛЕННЕЕ (меньшее значение м/с)
- Для восстановительного бега: targetType.workoutTargetTypeKey = "no.target", targetValueOne/Two = null
- Дистанция в метрах (endConditionValue)
- stepOrder нумеруется с 1, последовательно по всем шагам включая шаги внутри RepeatGroup
- childStepId у шагов внутри RepeatGroup = 1, у верхнеуровневых шагов = null

ПРИМЕР структуры для "12 по 500/300 м" в темпе 4:20–4:34 мин/км:
```json
{{
  "workoutName": "DD_{date_compact}-{group}_lvl",
  "description": null,
  "sportType": {{"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}},
  "subSportType": null,
  "workoutSegments": [{{
    "segmentOrder": 1,
    "sportType": {{"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}},
    "poolLengthUnit": null,
    "poolLength": null,
    "workoutSteps": [
      {{
        "type": "RepeatGroupDTO",
        "stepOrder": 1,
        "stepType": {{"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6}},
        "childStepId": null,
        "numberOfIterations": 12,
        "workoutSteps": [
          {{
            "type": "ExecutableStepDTO",
            "stepOrder": 2,
            "stepType": {{"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3}},
            "childStepId": 1,
            "description": null,
            "endCondition": {{"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": true}},
            "endConditionValue": 500.0,
            "preferredEndConditionUnit": null,
            "endConditionCompare": null,
            "targetType": {{"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}},
            "targetValueOne": 3.8461538,
            "targetValueTwo": 3.6496351,
            "targetValueUnit": null,
            "zoneNumber": null,
            "secondaryTargetType": null,
            "secondaryTargetValueOne": null,
            "secondaryTargetValueTwo": null,
            "secondaryTargetValueUnit": null,
            "secondaryZoneNumber": null,
            "endConditionZone": null,
            "strokeType": {{"strokeTypeId": 0, "strokeTypeKey": null, "displayOrder": 0}},
            "equipmentType": {{"equipmentTypeId": 0, "equipmentTypeKey": null, "displayOrder": 0}},
            "category": null, "exerciseName": null, "workoutProvider": null,
            "providerExerciseSourceId": null, "weightValue": null, "weightUnit": null
          }},
          {{
            "type": "ExecutableStepDTO",
            "stepOrder": 3,
            "stepType": {{"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4}},
            "childStepId": 1,
            "description": null,
            "endCondition": {{"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": true}},
            "endConditionValue": 300.0,
            "preferredEndConditionUnit": null,
            "endConditionCompare": null,
            "targetType": {{"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}},
            "targetValueOne": null,
            "targetValueTwo": null,
            "targetValueUnit": null,
            "zoneNumber": null,
            "secondaryTargetType": null,
            "secondaryTargetValueOne": null,
            "secondaryTargetValueTwo": null,
            "secondaryTargetValueUnit": null,
            "secondaryZoneNumber": null,
            "endConditionZone": null,
            "strokeType": {{"strokeTypeId": 0, "strokeTypeKey": null, "displayOrder": 0}},
            "equipmentType": {{"equipmentTypeId": 0, "equipmentTypeKey": null, "displayOrder": 0}},
            "category": null, "exerciseName": null, "workoutProvider": null,
            "providerExerciseSourceId": null, "weightValue": null, "weightUnit": null
          }}
        ],
        "endConditionValue": 12.0,
        "preferredEndConditionUnit": null,
        "endConditionCompare": null,
        "endCondition": {{"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": false}},
        "skipLastRestStep": false,
        "smartRepeat": false
      }}
    ]
  }}]
}}
```

Возьми темп из РЕКОМЕНДУЕМЫЙ ТЕМП (приоритет) и/или описания выбранной группы.
Верни ТОЛЬКО JSON в блоке ```json```, без пояснений."""

    try:
        response = _get_client().chat.completions.create(
            model=MODEL_FAST,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0,
        )
        raw = (response.choices[0].message.content or "").strip()
        # Извлекаем JSON из блока ```json ... ```
        m = _re.search(r'```json\s*([\s\S]*?)```', raw)
        if m:
            raw = m.group(1).strip()
        else:
            # Пробуем найти объект напрямую
            m2 = _re.search(r'\{[\s\S]*\}', raw)
            if m2:
                raw = m2.group(0)
        result = json.loads(raw)
        print(f"[garmin_json] OK: {result.get('workoutName', '?')}")
        return result
    except Exception as e:
        print(f"[garmin_json] Ошибка: {e}")
        return None


def _shorten_group_label(g: str) -> str:
    """Return ≤13-char label for suitability table rows."""
    _NAMED = {
        "красивые": "Красивые",
        "беговой релакс": "Релакс",
        "здоровья": "Здоровья",
    }
    s = g.strip()
    # "Группа N" → "Гр.N"
    import re as _re
    m = _re.match(r'[Гг]руппа\s+(\S+)', s)
    if m:
        return f"Гр.{m.group(1)}"
    # "N КРАСИВЫЕ" or "N Беговой Релакс" etc.
    m2 = _re.match(r'(\d+)\s+(.*)', s)
    if m2:
        num = m2.group(1)
        rest = m2.group(2).strip().lower()
        for key, short in _NAMED.items():
            if key in rest:
                return f"Гр.{num} {short}"
        return f"Гр.{num} {m2.group(2).strip()[:8]}"
    return s[:13]


def _sanitize_group_name(name: str) -> str:
    """Extract only the group number from any model-generated group label."""
    import re as _re
    s = str(name).strip()
    # Remove "Группа" / "группа" prefix
    s = _re.sub(r'[Гг]руппа\s*', '', s).strip()
    # Take only the first token (number, possibly with decimal like "3.5")
    m = _re.match(r'[\d]+(?:[.,][\d]+)?', s)
    return m.group(0).replace(',', '.') if m else s[:5]


def _pct_bar(pct: int, width: int = 8, color: str = '🟩') -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return color * filled + '⬜' * (width - filled)



def _suit_comment(g: dict, main_num: str) -> str:
    """Короткая словесная метка для шкалы подходимости (≤12 симв.)."""
    if str(g.get("number")) == main_num:
        return "оптимально"
    tag = (g.get("tag") or "").lower()
    if "риск" in tag:
        return "риск схода"
    if "тяжело" in tag or "перебор" in tag:
        return "тяжело"
    if "слишком легко" in tag:
        return "оч.легко"
    if "низкий" in tag:
        return "легко"
    pct = g.get("pct") or 0
    return "хорошо" if pct >= 60 else "запасной"


def _recovery_warning(analysis: dict, main_num: str, recovery: dict | None) -> str:
    """Строка для блока ⚡ Активное восстановление: метрики + восстановление меж отрезками."""
    rec_pace, active = None, None
    for grp in (analysis.get("groups") or []):
        if str(grp.get("number")) == str(main_num):
            for b in (grp.get("blocks") or []):
                if b.get("recovery_pace"):
                    rec_pace, active = b.get("recovery_pace"), b.get("active_recovery")
                    break
            break
    parts = []
    if recovery:
        m = []
        rv = recovery.get("recovery_score")
        if rv is not None:
            m.append(f"Recovery {rv}")
        tr = recovery.get("training_readiness")
        if isinstance(tr, dict) and tr.get("score") is not None:
            m.append(f"Readiness {tr['score']}")
        elif isinstance(tr, (int, float)):
            m.append(f"Readiness {tr}")
        hrv = recovery.get("hrv")
        if hrv is not None:
            m.append(f"HRV {hrv}")
        if m:
            parts.append(", ".join(m) + " — восстановление учтено")
    if rec_pace:
        kind = "активное (трусцой)" if active else "полный отдых"
        parts.append(f"восстановление между отрезками ~{rec_pace}/км — {kind}")
    return ". ".join(parts)


def recommendation_to_advice(rec: dict, analysis: dict, recovery: dict | None) -> dict:
    """Преобразует выход recommend_group + кэш-анализ + восстановление в advice-dict
    для format_evening_message — старая вёрстка v0.18.2 на новом (двухшаговом) движке."""
    groups = rec.get("groups") or []
    main = rec.get("main_group") or {}
    main_num = str(main.get("number", "—"))

    suitability, main_pace = [], ""
    for g in groups:
        if g.get("pct") is not None:
            item = {"group": g.get("number"), "percentage": g.get("pct"),
                    "comment": _suit_comment(g, main_num)}
            # Значимая альтернатива (та же пара, что в блоке «Альтернативы»): второй ряд в шкале
            ap, asp = g.get("alt_pct"), g.get("alt_spec")
            if ap is not None and asp and (ap - g["pct"]) >= 12:
                item["alt_pct"] = ap
                item["alt_label"] = _SPEC_LABELS.get(asp, asp)
            suitability.append(item)
        if str(g.get("number")) == main_num:
            main_pace = g.get("work_pace") or ""

    # Соседи по темпу для «оцени на разминке»
    paced = [(g, _pace_sec(g.get("work_pace"))) for g in groups if _pace_sec(g.get("work_pace"))]
    paced.sort(key=lambda x: x[1])
    nums = [str(g.get("number")) for g, _ in paced]
    if_good = if_tired = gap_note = ""
    if main_num in nums:
        i = nums.index(main_num)
        if i > 0:
            f = paced[i - 1][0]
            if_good = f"перейди в Группу {f.get('number')} ({f.get('work_pace')})"
        if i + 1 < len(paced):
            s = paced[i + 1][0]
            if_tired = f"снизься в Группу {s.get('number')} ({s.get('work_pace')})"
        gaps = []
        if i > 0:
            gaps.append(abs(paced[i][1] - paced[i - 1][1]))
        if i + 1 < len(paced):
            gaps.append(abs(paced[i + 1][1] - paced[i][1]))
        if gaps:
            gap_note = f"разрыв между соседними группами ~{int(sum(gaps) / len(gaps))} сек/км"

    # Обоснование
    zone_disp = main.get("zone_disp") or main.get("zone_label") or ""
    char_phrase = {
        "speed": "тренировка скоростная (короткие отрезки) — развивает скорость и МПК",
        "vo2": "интервалы на уровне МПК",
        "tempo": "длинные/пороговые отрезки — развивают ПАНО и темповую выносливость",
    }.get(rec.get("workout_character"), "")
    rp = [p for p in (
        f"{zone_disp} по твоим зонам" if zone_disp else "",
        char_phrase,
        f"оптимум по объёму качественной работы ({main.get('pct')}%)" if main.get("pct") is not None else "",
    ) if p]
    reason = (". ".join(rp) + ".") if rp else (analysis.get("overall_purpose") or "")

    # Подготовка из прозы Шага 1
    tips = []
    cn = (analysis.get("coach_notes") or "").strip()
    if cn:
        tips.append(cn if len(cn) <= 230 else cn[:227] + "…")
    wtw = (analysis.get("what_to_watch") or "").strip()
    if wtw:
        tips.append(f"Внимание: {wtw if len(wtw) <= 200 else wtw[:197] + '…'}")

    return {
        "recommended_group": main_num,
        "recommended_pace": main_pace,
        "reason": reason,
        "suitability_percentages": suitability,
        "spec_label": rec.get("specialization_label"),
        "if_feeling_good": if_good,
        "if_tired": if_tired,
        "gap_note": gap_note,
        "preparation_tips": tips,
        "warning": _recovery_warning(analysis, main_num, recovery),
        "overall_purpose": (analysis.get("overall_purpose") or "").strip(),
        "workout_summary": (analysis.get("summary") or "").strip(),
    }


def _build_step2_prompt(facts: dict, mode: str, long: bool) -> str:
    """Промпт Шага 2: модель ОБЪЯСНЯЕТ посчитанное (числа не меняет), формулирует живой текст."""
    calc = (mode == "calc")
    p = [
        "Ты — беговой тренер клуба Dusty Dumbbells. Пиши живым тёплым тоном, по-русски, кратко.",
        "ВАЖНО: числа уже посчитаны формулами — НЕ меняй и НЕ выдумывай группы/темпы/проценты, "
        "только объясни и сформулируй текст.",
    ]
    # Имя спортсмена — чтобы модель не выдумывала
    athlete_name = facts.get("athlete_name")
    if athlete_name:
        p.append(f"Имя спортсмена: {athlete_name} (обращайся только по этому имени, не выдумывай других).")
    if not calc:
        p.append(
            "Логика выбора (рассуждай в ней):\n"
            "- Оптимум группы = максимум объёма качественной работы = интенсивность × время до отказа.\n"
            "- Слишком быстро → мало времени работы, риск схода/травмы. Слишком медленно → нет интенсивности.\n"
            "- Оптимум — у порога с лёгким заходом выше; группа должна быть посильна ВСЮ тренировку.\n"
            "- Специализация смещает акцент среди посильных групп (5к/10к→МПК/скорость; "
            "полумарафон/марафон→ПАНО/темп; speed→скорость; fitness→умеренное)."
        )
    f = facts
    p.append("ПОСЧИТАНО (не менять):")
    p.append(f"- Рекомендованная группа: {f.get('group')}, темп {f.get('pace')}, "
             f"зона {f.get('zone')}, подходимость {f.get('pct')}%")
    if long:
        strat = "прогрессия" if f.get("strategy") == "progressive" else "ровный темп"
        sp = f", вторая половина {f.get('second_half_pace')}" if f.get("second_half_pace") else ""
        p.append(f"- Стратегия: {strat}; первая половина {f.get('first_half_pace')}{sp}")
    if f.get("suitability"):
        p.append("- Шкала групп: " + ", ".join(
            f"гр.{s['group']} {s['percentage']}%" for s in f["suitability"][:8]))
    if f.get("specialization"):
        p.append(f"- Специализация бегуна: {f.get('specialization')}")
    if f.get("character"):
        p.append(f"- Характер тренировки: {f.get('character')}")
    if f.get("recovery"):
        p.append(f"- Восстановление сегодня: {f.get('recovery')}")
    if f.get("overall_purpose"):
        p.append(f"- Что развивает тренировка: {f.get('overall_purpose')}")
    if f.get("block_contrast"):
        p.append(f"- Контраст блоков: {f.get('block_contrast')}")
    if calc:
        p.append("Задача: опиши посчитанный результат понятным языком — почему эта группа "
                 "(по % и зоне), что делать. Без переусложнения, 1-3 предложения.")
    else:
        what = " и эта стратегия" if long else ""
        p.append(f"Задача: объясни ПОЧЕМУ именно эта группа{what} оптимальны — в логике выше, "
                 "на данных бегуна. Тёплый тренерский тон, 2-4 предложения.")
    schema = '{"reason": "почему эта группа"'
    if long:
        schema += ', "strategy_reason": "почему такая стратегия"'
    schema += "}"
    p.append(f"Верни строго JSON: {schema}\nОтвечай только JSON.")
    return "\n".join(p)


def generate_step2_prose(facts: dict, mode: str = "smart", long: bool = False) -> tuple[dict, dict]:
    """Генерирует ИИ-прозу обоснования (Шаг 2) поверх посчитанных формулами чисел.
    Возвращает ({reason, strategy_reason?}, stats). При сбое — ({}, stats) (фолбэк на шаблон).
    """
    import re as _re
    if mode == "deep":
        model, max_tok, to = MODEL_DEEP, 1600, 180
    elif mode == "fast":
        model, max_tok, to = MODEL_FAST, 1200, 60
    elif mode == "calc":
        model, max_tok, to = MODEL_FAST, 900, 60
    else:
        model, max_tok, to = MODEL_SMART, 1400, 120

    prompt = _build_step2_prompt(facts, mode, long)
    t0 = _time.time()
    stats = {"time_sec": 0, "input_tokens": None, "output_tokens": None, "mode": mode}
    try:
        resp = _get_client().chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tok, temperature=0.3, timeout=to)
        usage = resp.usage
        stats = {"time_sec": round(_time.time() - t0, 1),
                 "input_tokens": usage.prompt_tokens if usage else None,
                 "output_tokens": usage.completion_tokens if usage else None, "mode": mode}
        msg = resp.choices[0].message
        raw = (msg.content or "").strip()
        reasoning = getattr(msg, "reasoning_content", None)
        if not raw and reasoning:
            m = _re.search(r'\{[\s\S]*\}', reasoning)
            if m:
                raw = m.group(0)
        raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).replace("```json", "").replace("```", "").strip()
        prose = {}
        try:
            prose = json.loads(raw)
        except Exception:
            m = _re.search(r'\{[\s\S]*\}', raw)
            if m:
                try:
                    prose = json.loads(m.group(0))
                except Exception:
                    prose = {}
        if not isinstance(prose, dict):
            prose = {}
        logger.info(f"step2 prose: mode={mode}, time={stats['time_sec']}s, "
                    f"tokens={stats['input_tokens']}/{stats['output_tokens']}, "
                    f"reason={'y' if prose.get('reason') else 'n'}")
        return prose, stats
    except Exception as e:
        logger.warning(f"step2 prose error (mode={mode}): {e}")
        stats["time_sec"] = round(_time.time() - t0, 1)
        return {}, stats


def _sec_to_pace(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _recovery_descriptor(recovery: dict | None) -> str:
    """Словесное описание восстановления (Whoop/Garmin Readiness/HRV/сон) для long-обоснования."""
    if not recovery:
        return ""
    parts = []
    rv = recovery.get("recovery_score")
    if rv is not None:
        parts.append(f"Recovery {rv}")
    tr = recovery.get("training_readiness")
    if isinstance(tr, dict) and tr.get("score") is not None:
        parts.append(f"Readiness {tr['score']}")
    elif isinstance(tr, (int, float)):
        parts.append(f"Readiness {tr}")
    hrv = recovery.get("hrv")
    if hrv is not None:
        parts.append(f"HRV {hrv}")
    sleep = recovery.get("sleep_hours") or recovery.get("sleep")
    if sleep is not None:
        parts.append(f"сон {sleep}")
    return ", ".join(parts)


def recommendation_to_long_advice(rec: dict, analysis: dict, recovery: dict | None) -> dict:
    """Выход recommend_long + кэш-анализ + восстановление → advice-dict для
    format_long_run_message (старая вёрстка /long v0.18.2). Стратегия (ровный/прогрессия)
    выводится ИЗ данных восстановления."""
    import re as _re
    main = rec.get("main_group") or {}
    main_num = str(main.get("number", "—"))
    first_pace = main.get("pace") or "—"

    suitability = [{"group": g["number"], "percentage": g["pct"], "comment": g.get("comment", "")}
                   for g in (rec.get("groups") or []) if g.get("pct") is not None]

    # Второй темп: из текста группы анализа (вторая половина), иначе первый − 30 сек/км
    second_pace = None
    for grp in (analysis.get("groups") or []):
        if str(grp.get("number")) == main_num:
            paces = _re.findall(r'\d{1,2}:\d{2}', str(grp.get("work") or ""))
            if len(paces) >= 2:
                second_pace = paces[1]
            break
    fp_sec = _pace_sec(first_pace)
    if second_pace is None and fp_sec:
        second_pace = _sec_to_pace(fp_sec - 30)

    # Стратегия из восстановления
    rv = _recovery_value(recovery)
    has_prog = bool(rec.get("has_progression") or analysis.get("has_progression"))
    desc = _recovery_descriptor(recovery)
    warning = ""
    if has_prog and rv is not None and rv >= 67:
        strategy = "progressive"
        strategy_reason = (
            f"{desc} — восстановление хорошее. Первую половину держи ровно, вторую можно "
            f"ускорить до ~{second_pace}/км." if desc else
            f"Восстановление хорошее — можно прогрессия со второй половины (до ~{second_pace}/км).")
    elif rv is not None and rv < 50:
        strategy, second_pace = "even", None
        strategy_reason = (
            f"{desc} — восстановление неполное. Держи ровный темп всю тренировку, не разгоняйся: "
            "цель сегодня — аэробный объём, а не нагрузка.")
        warning = "Восстановление неполное — при плохом самочувствии сократи дистанцию или сделай лёгкий кросс."
    else:
        strategy, second_pace = "even", None
        strategy_reason = (
            f"{desc or 'Нет свежих данных о восстановлении'} — рекомендую ровный темп. "
            "Прогрессия (вторую половину быстрее) — по желанию и самочувствию.")

    # Соседи для «оцени на разминке»
    if_good = if_tired = ""
    for a in (rec.get("alternatives") or []):
        if a.get("role") == "step_up" and not if_good:
            if_good = f"перейди в Группу {a.get('number')} (~{a.get('pace')}/км)"
        elif a.get("role") == "step_down" and not if_tired:
            if_tired = f"снизься в Группу {a.get('number')} (~{a.get('pace')}/км)"
    if not if_tired:
        for a in (rec.get("alternatives") or []):
            if a.get("role") == "health":
                if_tired = f"Группа {a.get('number')} (бег/ходьба)"
                break

    # Подготовка (long-специфика: разминка, вода, питание, одежда)
    tips = []
    cn = (analysis.get("coach_notes") or "").strip()
    if cn:
        tips.append(cn if len(cn) <= 230 else cn[:227] + "…")
    tips.append("💧 Вода: 100 минут бега — спланируй питьё (возьми воду / маршрут с источниками)")
    tips.append("🍌 Питание: за 1–1.5 ч лёгкий углеводный перекус; на длинной можно гель/изотоник")
    tips.append("👕 Одежда по погоде — на длительной учитывай ветер и похолодание к концу")

    return {
        "recommended_group": main_num,
        "suitability_percentages": suitability,
        "run_strategy": strategy,
        "first_half_pace": first_pace,
        "second_half_pace": second_pace,
        "strategy_reason": strategy_reason,
        "if_feeling_good": if_good,
        "if_tired": if_tired,
        "preparation_tips": tips,
        "warning": warning,
    }


def format_evening_message(advice: dict, workout: dict, stats: dict | None = None, weather_line: str = "", profile_only: bool = False, has_tracker: bool = True) -> str:
    if not advice:
        return "Не удалось получить рекомендацию. Попробуй позже."

    suitability = advice.get("suitability_percentages") or []
    if suitability:
        best = max(suitability, key=lambda x: x.get("percentage", 0))
        group = str(best.get("group", advice.get("recommended_group", "—")))
    else:
        group = advice.get("recommended_group", "—")
    pace = advice.get("recommended_pace", "")
    reason = _html.escape(advice.get("reason", ""))
    if_good = _html.escape(advice.get("if_feeling_good", ""))
    if_tired = _html.escape(advice.get("if_tired", ""))
    gap_note = _html.escape(advice.get("gap_note", ""))
    tips = [_html.escape(t) for t in (advice.get("preparation_tips") or [])]
    warning = advice.get("warning")

    workout_type = workout.get("workout_type", "unknown")
    type_emoji = {'interval': '⚡', 'long': '🕐', 'hills': '⛰️'}.get(workout_type, '🏃')
    is_past = workout.get("is_past", False)
    sep = "━━━━━━━━━━━━━━━"

    raw_date = workout.get("workout_date", "")
    try:
        from datetime import datetime as _dt
        date_fmt = _dt.strptime(raw_date, "%Y-%m-%d").strftime("%d.%m")
    except Exception:
        date_fmt = raw_date

    if is_past:
        lines = [
            "⏳ <b>Новый анонс ещё не вышел</b>",
            f"Последняя тренировка — {workout.get('weekday', '').capitalize()} {date_fmt}",
            f"📍 {_html.escape(workout.get('location', ''))}",
        ]
        if weather_line:
            lines.append(weather_line)
    else:
        schedule = workout.get('schedule', '').replace(chr(10), '  |  ')
        location = _html.escape(workout.get('location', ''))
        lines = [
            f"{type_emoji} <b>Тренировка {date_fmt}</b>",
            f"📍 {location}",
        ]
        if schedule:
            lines.append(f"⏰ {_html.escape(schedule)}")
        if weather_line:
            lines.append(weather_line)

    if stats and stats.get("mode") == "fast" and not profile_only:
        lines.append("🔥 Быстрый режим — без глубокого анализа")

    if not has_tracker:
        lines.append("⚠️ Рекомендация без данных о нагрузке — подключи Garmin, COROS или Polar для точного анализа")

    lines.append(sep)

    # О тренировке (Цель + Суть) — ДО подходимости
    overall = _html.escape((advice.get("overall_purpose") or "").strip())
    summary = _html.escape((advice.get("workout_summary") or "").strip())
    work_text = _html.escape((workout.get("work_text") or "").strip())
    if work_text:
        lines.append(f"🚪 Работа: {work_text}")
    if summary:
        lines.append("<b>💡 Суть</b>")
        lines.append(f"<i>{summary}</i>")
    if overall:
        lines.append("<b>🏁 Цель тренировки</b>")
        lines.append(f"<i>{overall}</i>")
    if overall or summary or work_text:
        lines.append(sep)

    # Процентная шкала — перед основной рекомендацией
    if suitability:
        sorted_s = sorted(suitability, key=lambda x: x.get("percentage", 0), reverse=True)
        lines.append("📊 <b>Подходимость групп:</b>")
        has_dual = False
        for item in sorted_s:
            g = _sanitize_group_name(str(item.get("group", "?")))
            pct = int(item.get("percentage", 0))
            comment = (item.get('comment') or '')[:12]
            comment_str = f" — {_html.escape(comment)}" if comment else ""
            lines.append(f"<code>Гр.{g:<4} {_pct_bar(pct)} {pct:>3}%{comment_str}</code>")
            # Второй ряд (🟦) — если есть значимая альтернатива (другая цель)
            alt_pct = item.get("alt_pct")
            if alt_pct is not None:
                has_dual = True
                alt = int(alt_pct)
                alt_label = (item.get('alt_label') or '')[:12]
                alt_str = f" — {_html.escape(alt_label)}" if alt_label else ""
                lines.append(f"<code>{'':<8}{_pct_bar(alt, color='🟦')} {alt:>3}%{alt_str}</code>")
        if has_dual:
            spec_label = _html.escape(advice.get("spec_label") or "твоя цель")
            legend_alt = next((it.get('alt_label') for it in sorted_s
                               if it.get('alt_pct') is not None and it.get('alt_label')), "другая цель")
            lines.append(f"🟩 — по твоим силам сегодня (цель: {spec_label})")
            lines.append(f"🟦 — ценность этой зоны если бы цель была: {_html.escape(legend_alt)}")
        lines.append(sep)

    # Основная рекомендация
    pace_str = f" — {_html.escape(pace)}" if pace else ""
    lines.append(f"🎯 <b>Группа {group}{pace_str}</b>")
    if reason:
        lines.append(f"<i>{reason}</i>")

    # Классифицируем warning: "восстановл" / "300м" / "темп ~" → сразу после группы, остальные → внизу
    warning_str = str(warning).strip() if warning and str(warning).lower() not in ("null", "none", "") else ""
    _recovery_kw = ("восстановл", "300м", "темп ~")
    is_recovery_warning = bool(warning_str and any(kw in warning_str.lower() for kw in _recovery_kw))

    # ⚡ Активное восстановление — сразу после группы
    if is_recovery_warning:
        lines.append(f"\n⚡ <b>Активное восстановление</b>\n<i>{_html.escape(warning_str)}</i>")

    # Оцени на разминке (+ gap_note внутри блока)
    if if_good or if_tired or gap_note:
        lines += [sep, "<b>Оцени на разминке:</b>"]
        if if_good:
            lines.append(f"🟢 Легко → {if_good}")
        if if_tired:
            lines.append(f"🔴 Тяжело → {if_tired}")
        if gap_note:
            lines.append(f"📐 {gap_note}")

    # Подготовка
    if tips:
        lines += [sep, "<b>Подготовка:</b>"]
        for tip in tips:
            lines.append(f"• {tip}")

    # ⚠️ Остальные предупреждения (соревнование, усталость) — перед «Удачи»
    if warning_str and not is_recovery_warning:
        lines.append(f"\n⚠️ <i>{_html.escape(warning_str)}</i>")

    lines.append("\n<i>Анонс следующей тренировки выходит накануне утром.</i>" if is_past
                 else "\nУдачи на тренировке! 💪\nНо главное помни:\nПриходи - не бойся, уходи - не плачь!")

    if stats:
        t = stats.get("time_sec", "?")
        inp = stats.get("input_tokens", "?")
        out = stats.get("output_tokens", "?")
        mode_str = _MODE_LABELS.get(stats.get("mode"), "⚡ Умное")
        lines.append(f"\n<i>⏱ {t}с | {mode_str} | 📥 {inp} / 📤 {out} | v{VERSION}</i>")

    return '\n'.join(lines)


def format_morning_message(advice: dict, last_rec: dict | None = None) -> str:
    if not advice:
        return "Не удалось получить рекомендацию. Доверяй своим ощущениям!"

    status = advice.get("status", "go")
    group = _html.escape(str(advice.get("recommended_group", "—"))).replace("Группа ", "")
    message = _html.escape(advice.get("message", ""))
    adjustments = advice.get("adjustments") or []

    status_emoji = {'go': '✅', 'adjust': '🟡', 'rest': '🔴'}.get(status, '✅')
    status_text = {
        'go': 'Отличное восстановление — идём по плану!',
        'adjust': 'Восстановление неполное — корректируем план',
        'rest': 'Лучше отдохнуть или сделать лёгкую пробежку'
    }.get(status, '')

    lines = []
    if last_rec and last_rec.get("recommended_group"):
        eve_group = _html.escape(str(last_rec["recommended_group"]))
        eve_pace = _html.escape(str(last_rec.get("recommended_pace", "")))
        pace_str = f" — {eve_pace}" if eve_pace else ""
        lines.append(f"📋 <i>Вечерняя рекомендация: Группа {eve_group}{pace_str}</i>")
        lines.append("")

    lines += [
        f"{status_emoji} <b>{status_text}</b>",
        "",
        message,
    ]

    if status != 'rest':
        lines.append(f"\nГруппа сегодня: <b>{group}</b>")

    if adjustments:
        lines.append("\nСоветы на сегодня:")
        for adj in adjustments:
            if adj:
                lines.append(f"• {_html.escape(str(adj))}")

    return '\n'.join(lines)


def _pace_to_sec(pace: str) -> int:
    parts = pace.split(':')
    return int(parts[0]) * 60 + int(parts[1])


def _add_sec_to_pace(pace: str, seconds: int) -> str:
    try:
        total = _pace_to_sec(pace) + seconds
        return f"{total // 60}:{total % 60:02d}"
    except Exception:
        return "—"


def build_long_run_prompt(workout: dict, fitness: dict, recovery: dict | None = None, weather_prompt: str = "") -> str:
    groups = workout.get("groups") or []
    groups_lines = []
    for g in groups:
        if g.get("label"):
            num_prefix = f"{g['number']} " if g.get("number") else ""
            label = f"{num_prefix}{g['label']}"
        else:
            label = f"Группа {g.get('number', '?')}"
        p_start = g.get("pace_start")
        p_end = g.get("pace_end")
        prog = g.get("progression", False)
        if p_start and p_end and prog:
            groups_lines.append(f"{label}: первые 50 мин {p_start} мин/км → вторые 50 мин {p_end} мин/км (прогрессия)")
        elif p_start:
            groups_lines.append(f"{label}: {p_start} мин/км (ровный темп)")
        else:
            groups_lines.append(label)

    groups_text = "\n".join(groups_lines) if groups_lines else (workout.get("groups_raw") or "—")[:800]
    even_note = "Примечание: внутри каждой группы доступен выбор — прогрессия или ровный темп." if workout.get("even_pace_available") else ""

    vo2max, vo2max_source = _estimate_vo2max(fitness, recovery)
    vo2max_line = f"VO2max: {vo2max} мл/кг/мин [{vo2max_source}]" if vo2max else "VO2max: нет данных"

    lt_pace = fitness.get("lactate_threshold_pace")
    lt_hr = fitness.get("lactate_threshold_hr")
    lt_line = ""
    if lt_pace:
        try:
            lt_slow = _add_sec_to_pace(lt_pace, 60)
            lt_very_slow = _add_sec_to_pace(lt_pace, 90)
            lt_range = f"При пороге {lt_pace} — целевой темп длительной {lt_slow}-{lt_very_slow} мин/км"
        except Exception:
            lt_range = "Темп первой половины должен быть на 60-90 сек/км медленнее порога"
        lt_line = (f"Лактатный порог: {lt_pace} мин/км"
                   + (f" при ЧСС {lt_hr}" if lt_hr else "")
                   + f"\n{lt_range}. Прогрессия: вторая половина на 30 сек/км быстрее первой."
                   + f"\nВАЖНО: first_half_pace = точный pace_start выбранной группы")

    load = fitness.get("training_load", {})
    load_text = load.get("summary", "") if load else ""
    tsb = load.get("tsb") if load else None

    recovery_source = (recovery or {}).get("source", "")
    recovery_score = (recovery or {}).get("recovery_score")
    hrv = (recovery or {}).get("hrv")
    sleep_hours = (recovery or {}).get("sleep_hours")
    sleep_score = (recovery or {}).get("sleep_score")
    body_battery = (recovery or {}).get("body_battery")
    training_readiness = (recovery or {}).get("training_readiness")

    recovery_block = _build_recovery_block(
        recovery_source, recovery_score, hrv, sleep_score, sleep_hours, body_battery, training_readiness
    ) if recovery else "Нет данных о восстановлении."

    last_race = fitness.get("last_race")
    race_text = ""
    if last_race and last_race.get("still_recovering"):
        race_text = (
            f"\nВНИМАНИЕ: восстановление после {last_race['name']} "
            f"({last_race['distance_km']} км, {last_race['days_since']} дней назад). "
            f"До {last_race['recovery_until']}. Рекомендуется ровный темп!"
        )

    gender = fitness.get("gender")
    gender_line = ("Пол: мужской" if gender == "male" else
                   "Пол: женский (нормы VO2max ниже на ~10%)" if gender == "female" else "")

    hints = []
    if recovery_score is not None:
        hints.append(f"Recovery: {recovery_score}% — {'≥67%, прогрессия возможна' if recovery_score >= 67 else '<67%, ровный темп предпочтительнее'}")
    if training_readiness and training_readiness.get("score") is not None:
        tr = training_readiness["score"]
        hints.append(f"Training Readiness: {tr} — {'≥70, прогрессия возможна' if tr >= 70 else '<70, осторожнее с прогрессией'}")
    if tsb is not None:
        hints.append(f"TSB: {tsb} — {'>-15, нет усталости' if tsb > -15 else '≤-15, накопленная усталость'}")
    if sleep_hours is not None:
        hints.append(f"Сон: {sleep_hours}ч — {'≥7ч' if sleep_hours >= 7 else '<7ч, недосып'}")
    strategy_hints = "\n".join(hints) if hints else "Нет данных для автоматического анализа."

    parts = [
        "Ты тренер бегового клуба Dusty Dumbbells.",
        "Помоги участнику подготовиться к воскресной длительной тренировке (100 минут).",
        "ВАЖНО: отвечай ТОЛЬКО на русском языке. Английский не использовать нигде — ни в комментариях, ни в советах, ни в причинах.",
        "",
        f"ТРЕНИРОВКА: {workout.get('workout_date', '—')} | {workout.get('location', '—')}",
    ]
    if workout.get("schedule"):
        parts.append(f"Расписание: {workout['schedule']}")
    parts += [
        f"Продолжительность: 100 минут ({workout.get('total_volume_km', '~18-22 км')})",
        "",
        "ГРУППЫ:",
        groups_text,
    ]
    if even_note:
        parts.append(even_note)
    parts += [
        "",
        "ДАННЫЕ СПОРТСМЕНА:",
        vo2max_line,
    ]
    if gender_line:
        parts.append(gender_line)
    if lt_line:
        parts.append(lt_line)
    if load_text:
        parts.append(f"Нагрузка: {load_text}")
    if fitness.get("summary"):
        parts.append(fitness["summary"])
    if race_text:
        parts.append(race_text)
    parts += [
        "",
        f"ДАННЫЕ ВОССТАНОВЛЕНИЯ (источник: {recovery_source.upper() if recovery_source else 'НЕТ'}):",
        recovery_block,
        "",
        "АНАЛИЗ ДЛЯ ВЫБОРА СТРАТЕГИИ:",
        strategy_hints,
        "",
        *(["ПОГОДА НА ТРЕНИРОВКУ:", weather_prompt,
           "Учитывай погоду при выборе стратегии:",
           "- Жара (>25°C): снизить темп первой половины на 10-20 сек/км, ровный темп предпочтительнее",
           "- Холод (<5°C): ровный или осторожный старт, мышцы разогреются только к 20-30 мин",
           "- Дождь или ветер >8 м/с: ровный темп, прогрессия рискованна",
           "- Жара + TSB < -20: обязательно ровный темп, снизить на группу",
           ""] if weather_prompt else []),
        "ПРАВИЛА:",
        "- Темп первой половины: на 60-90 сек МЕДЛЕННЕЕ лактатного порога (аэробная база)",
        "- Прогрессия рекомендуется: recovery ≥ 67% (или TR ≥ 70), TSB > -15, сон ≥ 7ч",
        "- Ровный темп: recovery < 67%, TSB ≤ -15 или восстановление после соревнования",
        "- При прогрессии: первые 50 мин = pace_start группы, вторые 50 мин = pace_end (на 30 сек/км быстрее)",
        "- ВАЖНО: first_half_pace = точный pace_start выбранной группы (не рассчитывай новый темп)",
        "- second_half_pace = pace_end выбранной группы (или null при ровном темпе)",
        "",
        "В suitability_percentages включи КАЖДУЮ группу из раздела ГРУППЫ (все без исключения).",
        "ВАЖНО: поле 'group' — ТОЛЬКО номер группы. Допустимо: '1', '2', '3', '3.5', '4', '5'.",
        "ЗАПРЕЩЕНО: 'Группа 3', '3 быстрая', '5 (537м)'. Только цифра или цифра с точкой.",
        "",
        "ЯЗЫК: все текстовые поля ТОЛЬКО на русском языке. Не используй английский.",
        "",
        "Дай ответ строго в формате JSON:",
        """{
  "recommended_group": "только номер группы (число, например 4)",
  "run_strategy": "progressive" или "even",
  "first_half_pace": "X:XX",
  "second_half_pace": "X:XX или null если ровный темп",
  "strategy_reason": "1-2 предложения НА РУССКОМ — почему именно эта группа и стратегия",
  "suitability_percentages": [ВСЕ группы из раздела ГРУППЫ, каждая обязательна:
    {"group": "только номер группы", "percentage": число_от_0_до_100, "comment": "СТРОГО 1-2 слова (примеры: идеально, запасной, слишком быстро, на грани, слишком легко, опасно)"}
  ],
  "if_feeling_good": "НА РУССКОМ — что делать если на разминке легко (например: перейди в группу 3)",
  "if_tired": "НА РУССКОМ — что делать если на разминке тяжело (например: останься в группе 4, не прогрессируй)",
  "preparation_tips": ["НА РУССКОМ — совет про питание/воду на 100 мин", "НА РУССКОМ — совет 2"],
  "warning": "НА РУССКОМ — предупреждение или null"
}""",
        "",
        "Отвечай только JSON, без лишнего текста. Все строки только на русском языке.",
    ]
    return "\n".join(parts)


def format_long_run_message(advice: dict, workout: dict, stats: dict | None = None, weather_line: str = "", profile_only: bool = False, has_tracker: bool = True) -> str:
    if not advice:
        return "Не удалось получить рекомендацию. Попробуй позже."

    sep = "━━━━━━━━━━━━━━━"

    raw_date = workout.get("workout_date", "")
    try:
        from datetime import datetime as _dt
        _dt_obj = _dt.strptime(raw_date, "%Y-%m-%d")
        date_fmt = _dt_obj.strftime("%d.%m")
        _WEEKDAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        weekday_str = " " + _WEEKDAYS_RU[_dt_obj.weekday()]
    except Exception:
        date_fmt = raw_date
        weekday_str = ""

    is_past = workout.get("is_past", False)
    location = _html.escape(workout.get("location") or "")
    schedule_raw = (workout.get("schedule") or "").replace('\n', '  |  ')

    lines = [f"🕐 <b>Long Run{weekday_str} {date_fmt}</b>" + (" ⏳" if is_past else "")]
    loc_parts = []
    if location:
        loc_parts.append(f"📍 {location}")
    if schedule_raw:
        loc_parts.append(f"⏰ {_html.escape(schedule_raw)}")
    if loc_parts:
        lines.append("  ".join(loc_parts))
    if weather_line:
        lines.append(weather_line)

    if stats and stats.get("mode") == "fast" and not profile_only:
        lines.append("🔥 Быстрый режим — без глубокого анализа")

    if not has_tracker:
        lines.append("⚠️ Рекомендация без данных о нагрузке — подключи Garmin, COROS или Polar для точного анализа")

    lines.append(sep)

    # Шкала подходимости
    suitability = advice.get("suitability_percentages") or []
    if suitability:
        sorted_s = sorted(suitability, key=lambda x: x.get("percentage", 0), reverse=True)
        lines.append("📊 <b>Подходимость групп:</b>")
        for item in sorted_s:
            g = _sanitize_group_name(str(item.get("group", "?")))
            pct = int(item.get("percentage", 0))
            bar = _pct_bar(pct)
            comment = (item.get('comment') or '')[:12]
            comment_str = f" — {_html.escape(comment)}" if comment else ""
            lines.append(f"<code>Гр.{g:<4} {bar} {pct:>3}%{comment_str}</code>")
        lines.append(sep)

    group_raw = str(advice.get("recommended_group", "—"))
    if group_raw.lower().startswith("группа "):
        group_display = _html.escape(group_raw)
    else:
        try:
            float(group_raw)
            group_display = f"Группа {_html.escape(group_raw)}"
        except ValueError:
            group_display = _html.escape(group_raw)
    strategy = advice.get("run_strategy", "even")
    first_pace = advice.get("first_half_pace") or "—"
    second_pace = advice.get("second_half_pace")
    strategy_reason = _html.escape(advice.get("strategy_reason") or "")

    # Block: recommended group
    lines.append(f"🎯 <b>{group_display} — {first_pace} мин/км</b>")
    # group_reason comes from strategy_reason only when it's about group choice; use it inline
    lines.append(sep)

    # Block: strategy
    if strategy == "progressive":
        lines.append("📈 <b>Стратегия: С прогрессией</b>")
        lines.append(f"Первые 50 мин: {first_pace} мин/км")
        if second_pace:
            lines.append(f"Вторые 50 мин: {second_pace} мин/км")
    else:
        lines.append("📊 <b>Стратегия: Ровный темп</b>")
        lines.append(f"Держи {first_pace} мин/км всю тренировку")
    if workout.get("even_pace_available"):
        lines.append("<i>(доступна опция без прогрессии внутри группы)</i>")
    if strategy_reason:
        lines.append("")
        lines.append(strategy_reason)

    if_good = _html.escape(advice.get("if_feeling_good") or "")
    if_tired = _html.escape(advice.get("if_tired") or "")
    if if_good or if_tired:
        lines += [sep, "<b>Оцени на разминке:</b>"]
        if if_good:
            lines.append(f"🟢 Легко → {if_good}")
        if if_tired:
            lines.append(f"🔴 Тяжело → {if_tired}")

    tips = [_html.escape(t) for t in (advice.get("preparation_tips") or [])]
    if tips:
        lines += [sep, "<b>Подготовка:</b>"]
        for tip in tips:
            lines.append(f"• {tip}")

    warning = advice.get("warning")
    if warning and str(warning).lower() not in ("null", "none", ""):
        lines.append(f"\n⚠️ <i>{_html.escape(str(warning))}</i>")

    lines.append("\nУдачи на длительной! 🏃")

    if stats:
        t = stats.get("time_sec", "?")
        inp = stats.get("input_tokens", "?")
        out = stats.get("output_tokens", "?")
        mode_str = _MODE_LABELS.get(stats.get("mode"), "⚡ Умное")
        lines.append(f"\n<i>⏱ {t}с | {mode_str} | 📥 {inp} / 📤 {out} | v{VERSION}</i>")

    return '\n'.join(lines)


if __name__ == "__main__":
    # Группы с БОЛЬШИМ разрывом между 3 и 4 (~50 сек/км)
    GROUPS_RAW = (
        "1️⃣ Группа\n4 км: 3:20/3:35/3:35/3:10\n400 м – лёгкий бег\n200 м – 38-33 сек\n"
        "2️⃣ Группа\n4 км: 3:35/3:50/3:50/3:25\n400 м – лёгкий бег\n200 м – 42-35 сек\n"
        "3️⃣ Группа\n4 км: 3:45/4:00/4:00/3:35\n400 м – лёгкий бег\n200 м – 45-38 сек\n"
        "4️⃣ Группа\n4 км: 4:25/4:50/4:50/4:15\n400 м – лёгкий бег\n200 м – 52-48 сек"
    )

    mock_workout = {
        "workout_date": "2026-05-22",
        "weekday": "пятница",
        "workout_type": "interval",
        "location": "Северный спортивный центр, Лужники",
        "schedule": "19:30 — сбор, 19:45 — старт разминки",
        "work_text": "4 км, 1 и 4 км быстрые вставки + 400 м легкий бег + 10 по 200/200 м",
        "groups_raw": GROUPS_RAW,
        "total_volume_km": "14 км",
        "extra_groups": [{"number": "3.5"}, {"number": "5"}],
        "extra_groups_raw": [
            "Группа 3.5\nРабота: 4 км\nПлан: темп 4:25/4:10/4:00\n200 м темп 48-40 сек",
            "5️⃣ Группа\nРазминка: 3,8 км\nРабота: 4 км: 4:50/5:15/5:15/4:40\n200м - 57-53",
        ],
        "is_past": False,
    }

    SCENARIOS = [
        {
            "label": "A: Garmin VO2max=54, свежий (TSB+4, Recovery 72%)",
            "fitness": {
                "summary": "За 2 недели: 5 тренировок, 62 км. Средний темп 4:45 мин/км.",
                "training_load": {"summary": "CTL=52, ATL=48, TSB=+4 — свежий"},
                "predictions": {
                    "5km":  {"time": "18:45", "pace": "3:45", "source": "actual"},
                    "10km": {"time": "39:10", "pace": "3:55", "source": "predicted"},
                },
                "last_race": None,
            },
            "recovery": {
                "vo2max": 54, "source": "garmin",
                "recovery_score": 72, "hrv": 58, "sleep_hours": 7.5, "body_battery": 80,
            },
        },
        {
            "label": "B: Strava оценка VO2max + профиль (лактатный порог 4:17/174), усталость",
            "fitness": {
                "summary": "За 2 недели: 4 тренировки, 52 км. Средний темп 4:50 мин/км.",
                "training_load": {"summary": "CTL=45, ATL=50, TSB=-5 — накопленная усталость"},
                "predictions": {
                    "5km":  {"time": "19:30", "pace": "3:54", "source": "predicted"},
                    "10km": {"time": "40:30", "pace": "4:03", "source": "predicted"},
                },
                "last_race": None,
                # Данные из /profile пользователя
                "vo2max": 53,
                "vo2max_source": "профиль",
                "lactate_threshold_pace": "4:17",
                "lactate_threshold_hr": 174,
            },
            "recovery": {
                "recovery_score": 45, "hrv": 42, "sleep_hours": 6.0,
                "body_battery": None, "source": "whoop",
            },
        },
    ]

    for s in SCENARIOS:
        print(f"\n{'='*65}")
        print(f"СЦЕНАРИЙ {s['label']}")
        print('='*65)

        vo2max, src = _estimate_vo2max(s["fitness"], s["recovery"])
        print(f"VO2max: {vo2max} [{src}]\n")

        prompt = build_evening_prompt(mock_workout, s["fitness"], s["recovery"])
        print("--- ПРОМПТ (первые 25 строк) ---")
        print("\n".join(prompt.splitlines()[:25]))

        print("\n--- GROQ ОТВЕТ ---")
        result = ask_groq(prompt)
        if result:
            advice = result["advice"]
            stats = result["stats"]
            print(json.dumps(advice, ensure_ascii=False, indent=2))
            print(f"\nStats: {stats}")
            print("\n--- ФИНАЛЬНОЕ СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЮ ---")
            print(format_evening_message(advice, mock_workout, stats=stats))
        else:
            print("Groq не ответил")