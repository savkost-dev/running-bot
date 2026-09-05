
# ── Слой 2: разбор текстовых ответов MCP в поля ──────────────────

def _kv(text: str) -> dict:
    """Строки вида 'Ключ: значение' из блока текста в словарь."""
    out = {}
    for line in (text or "").split("\n"):
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key and value:
            out[key] = value
    return out


def _num(value: str | None) -> float | None:
    """Число из строки. Отрицательные COROS отдаёт как признак «нет данных»."""
    if value is None:
        return None
    cleaned = value.replace("%", "").strip()
    try:
        num = float(cleaned)
    except ValueError:
        return None
    return None if num < 0 else num


def _pace_sec(value: str | None) -> int | None:
    """'4:16 /km' → 256 секунд на километр."""
    if not value:
        return None
    head = value.split("/")[0].strip()
    parts = head.split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def _time_sec(value: str | None) -> int | None:
    """'1:21:10' или '17:58' → секунды."""
    if not value:
        return None
    parts = value.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None


def parse_load(text: str | None) -> dict:
    """Нагрузка: берём самый свежий день. CTL = длинная, ATL = короткая."""
    if not text or "No training load" in text:
        return {}
    # Блоки по дням разделены пустой строкой; первый — самый свежий
    blocks = [b for b in text.split("\n\n") if "Load" in b]
    if not blocks:
        return {}
    first = blocks[0]
    date_line = next((l.strip() for l in first.split("\n")
                      if l.strip()[:4].isdigit()), None)
    data = _kv(first)
    return {
        "date": date_line,
        "atl": _num(data.get("Short-Term Load")),
        "ctl": _num(data.get("Long-Term Load")),
        "load_ratio": _num(data.get("Load Ratio")),
        "comment": data.get("Comment"),
    }


def parse_fitness(text: str | None) -> dict:
    """Форма: VO2max, беговой уровень, пороговый темп, прогнозы."""
    data = _kv(text or "")
    return {
        "vo2max": _num(data.get("VO2max")),
        "running_level": _num(data.get("Running Level")),
        "threshold_pace_sec": _pace_sec(data.get("Threshold Pace")),
        "predict_5k_sec": _time_sec(data.get("5 km Prediction")),
        "predict_10k_sec": _time_sec(data.get("10 km Prediction")),
        "predict_half_sec": _time_sec(data.get("Half Marathon Prediction")),
        "predict_marathon_sec": _time_sec(data.get("Marathon Prediction")),
    }


def parse_recovery(text: str | None) -> dict:
    """Восстановление: процент и словесный уровень. 'Unknown' — это нет данных."""
    data = _kv(text or "")
    level = data.get("Level")
    if level and level.strip().lower() == "unknown":
        level = None
    return {
        "recovery_pct": _num(data.get("Recovery")),
        "level": level,
        "full_recovery": data.get("Estimated Full Recovery"),
    }


def parse_raw(raw: dict) -> dict:
    """Слой 2: сырьё из raw_service_data → плоский набор полей."""
    raw = raw or {}
    out = {}
    out.update(parse_load(raw.get("queryTrainingLoadAssessment")))
    out.update(parse_fitness(raw.get("queryFitnessAssessmentOverview")))
    out.update(parse_recovery(raw.get("queryRecoveryStatus")))
    return out
