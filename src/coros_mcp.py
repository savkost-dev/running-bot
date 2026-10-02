"""
COROS MCP — слой 1: сырое чтение данных по новой схеме (OAuth, без пароля).

Ходит в COROS по протоколу MCP и кладёт ответы as is в raw_service_data
под именем сервиса "coros_mcp". Разбор — слой 2, здесь ничего не парсим.

Отдельно от старого coros.py: там свой формат (JSON внутреннего API),
здесь текстовые ответы MCP. Смешивать не стали намеренно.
"""
import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager

import aiohttp

import coros_oauth

logger = logging.getLogger(__name__)

SERVICE = coros_oauth.SERVICE          # "coros_mcp"
PROTOCOL_VERSION = "2025-06-18"

# Что забираем: нагрузка (CTL/ATL), форма (VO2max, пороговый темп, прогнозы),
# восстановление, HRV во сне, пульс покоя, сон и тренировки за последние дни.
# Этого набора хватает, чтобы полностью заменить старое подключение по паролю.
TOOLS = [
    ("queryTrainingLoadAssessment", {}),
    ("queryFitnessAssessmentOverview", {}),
    ("queryRecoveryStatus", {}),
    ("querySleepHrv", {}),
    ("queryRestingHeartRate", {}),
    ("querySleepOverview", {}),
    ("queryStressTimeSeries", {"days": 2}),
    ("querySportRecords", "RECENT_ACTIVITIES"),
]


def _parse_reply(text: str) -> dict:
    """Ответ приходит либо обычным JSON, либо потоком строк 'data: {...}'."""
    text = (text or "").strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            body = line[5:].strip()
            if body and body != "[DONE]":
                return json.loads(body)
    return {}


def _text_of(reply: dict) -> str | None:
    """Достаёт человекочитаемый текст из ответа MCP."""
    content = (reply.get("result") or {}).get("content") or []
    parts = [c.get("text", "") for c in content if c.get("type") == "text"]
    joined = "\n".join(p for p in parts if p)
    return joined or None


async def _rpc(session, token: str, method: str, params: dict, req_id: int) -> dict:
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    async with session.post(coros_oauth.MCP_URL, json=payload, headers=headers) as resp:
        text = await resp.text()
        if resp.status != 200:
            logger.error(f"COROS MCP {method} HTTP {resp.status}: {text[:200]}")
            return {}
    return _parse_reply(text)


def _tool_args(args):
    """Аргументы инструмента. Для тренировок нужно окно дат — считаем от сегодня."""
    if args != "RECENT_ACTIVITIES":
        return args
    from datetime import date, timedelta
    import fitness as _fit
    today = date.today()
    # 02.10.2026: окно 14 дней — для даты последней пробежки (фактор простоя);
    # parse_activities сам режет 48 часов, ему окно не мешает.
    return {
        "startDate": (today - timedelta(days=_fit.LAST_RUN_WINDOW_DAYS)).strftime("%Y%m%d"),
        "endDate": today.strftime("%Y%m%d"),
        "limit": 50,
    }


async def _call_tool(session, token: str, name: str, args, req_id: int) -> str | None:
    reply = await _rpc(session, token, "tools/call",
                       {"name": name, "arguments": _tool_args(args)}, req_id)
    return _text_of(reply)


@asynccontextmanager
async def _connect(db_user_id: int):
    """Общая часть связи: токен, сессия и приветствие (initialize).

    Отдаёт (session, token); (None, None) — если доступа нет или связь не поднялась.
    Поверх неё строятся тонкие функции: fetch_raw, fetch_lap_data.
    """
    token = await coros_oauth.ensure_valid_token(db_user_id)
    if not token:
        logger.info(f"COROS MCP: нет токена для user_id={db_user_id}")
        yield None, None
        return

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        init = await _rpc(session, token, "initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "DoDick", "version": "1.0"},
        }, 1)
        if not init:
            logger.error(f"COROS MCP: initialize не прошёл, user_id={db_user_id}")
            yield None, None
            return
        yield session, token


async def fetch_lap_data(db_user_id: int, label_id: str, sport_type: int) -> str | None:
    """Круги одной тренировки: сырой ответ queryActivityLapData as is, БЕЗ парсинга.

    None — если доступа нет или ответ пустой. Разбор — слой 2.
    """
    try:
        async with _connect(db_user_id) as (session, token):
            if not session:
                return None
            return await _call_tool(session, token, "queryActivityLapData",
                                    {"labelId": str(label_id),
                                     "sportType": int(sport_type)}, 2)
    except Exception as e:
        logger.error(f"COROS MCP fetch_lap_data error user_id={db_user_id}: {e}")
        return None


async def fetch_fit_bytes(db_user_id: int, label_id: str, sport_type: int) -> bytes | None:
    """FIT-файл одной тренировки: ссылка через queryActivityFitFileDownloadUrls, затем скачивание.
    Считается в дневной лимит FIT у COROS. None — нет доступа/ссылки/файла."""
    try:
        async with _connect(db_user_id) as (session, token):
            if not session:
                return None
            text = await _call_tool(session, token, "queryActivityFitFileDownloadUrls",
                                    {"labelId": str(label_id), "sportType": int(sport_type),
                                     "limit": 1}, 3)
            text = _decode(text) or ""
            m = re.search(r"https?://\S+", text)
            if not m:
                logger.info(f"COROS MCP: ссылки на FIT нет для label={label_id}: {text[:200]!r}")
                return None
            url = m.group(0).rstrip("\"'),.]")
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"COROS MCP: FIT не скачался, status={resp.status}")
                    return None
                return await resp.read()
    except Exception as e:
        logger.error(f"COROS MCP fetch_fit_bytes error user_id={db_user_id}: {e}")
        return None


def parse_fit_points(data: bytes) -> tuple[list, list]:
    """FIT → (pts, lap_starts). pts — секундный ряд в формате Garmin details [(t_ms, dist_m, hr, cad)],
    lap_starts — старты кругов из lap-сообщений FIT (epoch ms). Пустые списки — если не разобралось.
    18.09.2026: сначала fitdecode — файлы COROS с нестандартными полями читает только она
    (fit_tool и fitparse падают на «invalid field size»); fit_tool остаётся запасным."""
    pts, lap_starts = _parse_fit_fitdecode(data)
    if pts:
        return pts, lap_starts
    return _parse_fit_fittool(data)


def _parse_fit_fitdecode(data: bytes) -> tuple[list, list]:
    """Разбор FIT через fitdecode (терпим к нестандартным полям COROS)."""
    import io
    import warnings
    try:
        import fitdecode
    except ImportError as e:
        logger.error(f"COROS FIT: fitdecode недоступен: {e}")
        return [], []

    def _val(frame, name):
        try:
            return frame.get_value(name)
        except KeyError:
            return None

    pts, lap_starts = [], []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with fitdecode.FitReader(io.BytesIO(data)) as fit:
                for frame in fit:
                    if not isinstance(frame, fitdecode.FitDataMessage):
                        continue
                    if frame.name == "record":
                        ts, dist = _val(frame, "timestamp"), _val(frame, "distance")
                        if ts is None or dist is None:
                            continue
                        pts.append((ts.timestamp() * 1000.0, float(dist),
                                    _val(frame, "heart_rate"), _val(frame, "cadence")))
                    elif frame.name == "lap":
                        st = _val(frame, "start_time")
                        if st is not None:
                            lap_starts.append(st.timestamp() * 1000.0)
    except Exception as e:  # noqa: BLE001
        logger.error(f"COROS FIT (fitdecode): {type(e).__name__}: {str(e)[:160]}")
        return [], []
    return pts, lap_starts


def _parse_fit_fittool(data: bytes) -> tuple[list, list]:
    """Запасной разбор FIT через fit_tool (работает на файлах Garmin)."""
    try:
        from fit_tool.fit_file import FitFile
        from fit_tool.profile.messages.record_message import RecordMessage
        from fit_tool.profile.messages.lap_message import LapMessage
    except ImportError as e:
        logger.error(f"COROS FIT: fit_tool недоступен: {e}")
        return [], []
    try:
        fit = FitFile.from_bytes(data)
    except Exception as e:  # noqa: BLE001
        logger.error(f"COROS FIT: файл не разобрался: {e}")
        return [], []
    pts, lap_starts = [], []
    for rec in fit.records:
        msg = rec.message
        if isinstance(msg, RecordMessage):
            if msg.timestamp is None or msg.distance is None:
                continue
            pts.append((float(msg.timestamp), float(msg.distance), msg.heart_rate, msg.cadence))
        elif isinstance(msg, LapMessage) and msg.start_time is not None:
            lap_starts.append(float(msg.start_time))
    return pts, lap_starts


def _gmt_str(ms: float) -> str:
    """epoch ms → 'YYYY-MM-DDTHH:MM:SS' (UTC) — формат startTimeGMT у Garmin."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def attach_lap_starts(splits: dict, pts: list, lap_starts: list) -> None:
    """Проставляет startTimeGMT ручным кругам COROS (у них времени старта нет).
    Если число кругов FIT совпадает — берём их старты; иначе накопленные длительности от первой точки.
    Мутирует splits."""
    laps = (splits.get("lapDTOs") or []) if isinstance(splits, dict) else []
    if not laps:
        return
    if lap_starts and len(lap_starts) == len(laps):
        for lp, ms in zip(laps, lap_starts):
            lp["startTimeGMT"] = _gmt_str(ms)
        return
    if not pts:
        return
    t = pts[0][0]
    for lp in laps:
        lp["startTimeGMT"] = _gmt_str(t)
        t += float(lp.get("duration") or 0) * 1000.0


async def fetch_raw(db_user_id: int) -> dict | None:
    """Слой 1: сырые ответы COROS MCP as is, БЕЗ парсинга.

    Возвращает {имя_инструмента: текст ответа} и сохраняет в базу.
    None — если доступа нет или всё пришло пустым.
    """
    import database as db

    raw: dict = {}
    try:
        async with _connect(db_user_id) as (session, token):
            if not session:
                return None

            results = await asyncio.gather(*[
                _call_tool(session, token, name, args, n)
                for n, (name, args) in enumerate(TOOLS, start=2)
            ], return_exceptions=True)
    except Exception as e:
        logger.error(f"COROS MCP fetch_raw error user_id={db_user_id}: {e}")
        return None

    for (name, _), value in zip(TOOLS, results):
        raw[name] = None if isinstance(value, Exception) else value

    # В ответе про HRV после сводки идёт ряд замеров каждые 10 минут за неделю —
    # это десятки килобайт, которые мы не используем. Обрезаем до сводки.
    hrv_text = raw.get("querySleepHrv")
    if hrv_text:
        cut = hrv_text.find("Sleep HRV Time Series")
        if cut > 0:
            raw["querySleepHrv"] = hrv_text[:cut]

    if not any(v for v in raw.values()):
        logger.info(f"COROS MCP fetch_raw: пусто для user_id={db_user_id}")
        return None

    # 02.10.2026: дата последней пробежки из списка записей (фактор простоя, fitness.get_last_run)
    if raw.get("querySportRecords"):
        try:
            db.save_last_run_date(db_user_id, SERVICE,
                                  last_run_date_from_records(parse_sport_records(raw["querySportRecords"])))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"COROS MCP fetch_raw: дата последней пробежки не сохранилась: {e}")

    db.save_raw_service_data(db_user_id, SERVICE,
                             json.dumps(raw, ensure_ascii=False, default=str))
    got = [k for k, v in raw.items() if v]
    logger.info(f"COROS MCP fetch_raw: сохранено user_id={db_user_id} ({', '.join(got)})")
    return raw


# ── Слой 2: разбор текстовых ответов в поля ────────────────

def _decode(text):
    """COROS отдаёт текст в кавычках, с переносами вида \\n внутри — распаковываем."""
    if not text:
        return ""
    text = str(text).strip()
    if text.startswith('"'):
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text.replace("\\n", "\n")


def _kv(text: str) -> dict:
    """Строки вида 'Ключ: значение' из блока текста в словарь."""
    out = {}
    for line in _decode(text).split("\n"):
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key and value:
            out[key] = value
    return out


def _num(value):
    """Число из строки. Отрицательное у COROS — признак «нет данных», не значение."""
    if value is None:
        return None
    try:
        num = float(str(value).replace("%", "").strip())
    except ValueError:
        return None
    return None if num < 0 else num


def _pace_sec(value):
    """'4:16 /km' → 256 секунд на километр."""
    if not value:
        return None
    parts = str(value).split("/")[0].strip().split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def _time_sec(value):
    """'1:21:10' или '17:58' → секунды."""
    if not value:
        return None
    try:
        nums = [int(p) for p in str(value).strip().split(":")]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None


def parse_load(text) -> dict:
    """Нагрузка: берём самый свежий день. CTL — длинная, ATL — короткая."""
    text = _decode(text)
    if not text or "No training load" in text:
        return {}
    blocks = [b for b in text.split("\n\n") if "Load" in b and ":" in b]
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


def parse_fitness(text) -> dict:
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


def parse_recovery(text) -> dict:
    """Восстановление: процент и словесный уровень. 'Unknown' — это нет данных."""
    data = _kv(text or "")
    level = data.get("Level")
    if level and level.strip().lower() == "unknown":
        level = None
    return {
        "recovery_pct": _num(data.get("Recovery")),
        "recovery_level": level,
        "full_recovery": data.get("Estimated Full Recovery"),
    }


def parse_hrv(text) -> dict:
    """HRV во сне: берём самую свежую ночь — среднее и личную базу."""
    text = _decode(text)
    if not text or "No data" in text:
        return {}
    # Сводка идёт от свежей даты к старой — нужен первый блок с числом
    out = {}
    day = None
    for line in text.split("\n"):
        line = line.strip()
        if len(line) == 11 and line.endswith(":") and line[4] == "-" and line[7] == "-":
            day = line[:10]                      # "2026-09-29:" — день пробуждения
        elif line.startswith("HRV Avg:") and "hrv" not in out:
            out["hrv"] = _num(line.split(":", 1)[1].replace("ms", "").split("\u2014")[0])
            out["hrv_date"] = day
        elif line.startswith("Baseline:") and "hrv_baseline" not in out:
            out["hrv_baseline"] = _num(line.split(":", 1)[1].replace("ms", ""))
        if "hrv" in out and "hrv_baseline" in out:
            break
    return out


def parse_rhr(text) -> dict:
    """Пульс покоя: первая строка с числом — самая свежая дата."""
    text = _decode(text)
    if not text or "No data" in text and "bpm" not in text:
        return {}
    for line in text.split("\n"):
        if "bpm" not in line:
            continue
        value = _num(line.split(":", 1)[-1].replace("bpm", ""))
        if value:
            return {"rhr": int(value)}
    return {}


def _sleep_hours(value) -> float | None:
    """'7h 51min' → 7.85 часа."""
    if not value:
        return None
    hours = minutes = 0
    for part in str(value).split():
        if part.endswith("h"):
            hours = _num(part[:-1]) or 0
        elif part.endswith("min"):
            minutes = _num(part[:-3]) or 0
    total = float(hours) + float(minutes) / 60
    return round(total, 2) if total > 0 else None


def parse_sleep(text) -> dict:
    """Сон: берём самую свежую ночь. Записи идут от старых к новым — нужен последний."""
    text = _decode(text)
    if not text or "No sleep data" in text:
        return {}
    blocks = [b for b in text.split("\n\n") if "Sleep Score" in b or "Main Sleep" in b]
    if not blocks:
        return {}
    data = _kv(blocks[-1])
    window = data.get("Main Sleep Window") or ""          # "2026-09-29 01:01 - 2026-09-29 08:00"
    wake_at = window.split(" - ")[-1].strip() if " - " in window else None
    return {
        "sleep_score": int(_num(data.get("Sleep Score"))) if _num(data.get("Sleep Score")) else None,
        "sleep_hours": _sleep_hours(data.get("Main Sleep (asleep)") or data.get("Main Sleep")),
        "wake_at": wake_at,                                # время пробуждения последней ночи
    }


def parse_activities(text) -> dict:
    """Тренировки за последние дни: сколько, сколько километров, когда последняя.

    Записи идут от свежих к старым. Считаем только окно в 48 часов — как у старого COROS.
    """
    import time as _time

    text = _decode(text)
    if not text or "No sport records" in text:
        return {}

    now = int(_time.time())
    window = 48 * 3600
    sessions = 0
    total_km = 0.0
    last_start = None

    for block in text.split("\n\n"):
        if "Time Window" not in block:
            continue
        start = None
        for piece in block.replace("|", " ").split():
            if piece.startswith("startTimestamp="):
                start = _num(piece.split("=", 1)[1])
        if start is None or now - int(start) > window:
            continue
        sessions += 1
        if last_start is None or start > last_start:
            last_start = start
        data = _kv(block)
        dist = data.get("Duration", "")
        # Строка вида "20:18 | Distance: 3.23 km" или "5:43 | Distance: 1000 m"
        if "Distance:" in dist:
            tail = dist.split("Distance:", 1)[1].strip()
            value = _num(tail.split()[0]) if tail.split() else None
            if value is not None:
                total_km += value / 1000 if tail.endswith(" m") or " m" in tail else value

    if not sessions:
        return {}

    return {
        "sessions_48h": sessions,
        "total_km_48h": round(total_km, 2),
        "last_activity_hours_ago": round((now - int(last_start)) / 3600, 1) if last_start else None,
    }


# Группы кругов у COROS: 10 — автоматические километры, 2 — ручные отрезки (кнопка на часах),
# 11 — пятикилометровки, -1 — итог за тренировку. Для разбора нужны только ручные.
LAP_GROUP_MANUAL = 2


def parse_lap_data(text) -> dict | None:
    """Сырой ответ queryActivityLapData → ручные отрезки в формате {"lapDTOs": [...]}.

    Формат тот же, что у Garmin и у переходника Strava — чтобы рисовалки
    и разбор работали одинаково. Дистанция у COROS в сантиметрах — переводим в метры,
    каденс уже полный (удваивать не надо, в отличие от Strava).
    Разметка по плану (wktStepIndex, intensityType) — не здесь, а там, где есть план.

    None — если ответ пустой или ручных отрезков в нём нет.
    """
    text = _decode(text)
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        logger.error("COROS MCP parse_lap_data: ответ не разобрался как JSON")
        return None

    group = next((g for g in (data.get("lapGroups") or [])
                  if g.get("type") == LAP_GROUP_MANUAL), None)
    laps = (group or {}).get("laps") or []
    if not laps:
        return None

    out = []
    for lp in laps:
        dist_cm = _num(lp.get("distance"))
        out.append({
            "wktStepIndex": None,
            "intensityType": "",
            "distance": round(dist_cm / 100, 1) if dist_cm is not None else None,
            "duration": _num(lp.get("time")),
            "averageHR": _num(lp.get("avgHr")),
            "maxHR": _num(lp.get("maxHr")),
            "averageRunCadence": _num(lp.get("avgCadence")),
            "averagePower": _num(lp.get("avgPower")),
        })
    return {"lapDTOs": out}


async def fetch_sport_records(db_user_id: int, days: int = 30) -> str | None:
    """Список тренировок за последние N дней: сырой ответ querySportRecords as is.

    Отдельно от fetch_raw: там окно в три дня для ночного забора, здесь нужно
    широкое окно — искать DD-тренировку для разбора. None — если доступа нет или пусто.
    """
    from datetime import date, timedelta
    today = date.today()
    args = {
        "startDate": (today - timedelta(days=days)).strftime("%Y%m%d"),
        "endDate": today.strftime("%Y%m%d"),
        "limit": 50,
    }
    try:
        async with _connect(db_user_id) as (session, token):
            if not session:
                return None
            return await _call_tool(session, token, "querySportRecords", args, 2)
    except Exception as e:
        logger.error(f"COROS MCP fetch_sport_records error user_id={db_user_id}: {e}")
        return None


def parse_sport_records(text) -> list:
    """Список тренировок → [{name, label_id, sport_type, date, start_ts, distance_m, duration_s}]
    в порядке ответа. date — 'YYYY-MM-DD' из заголовка записи, start_ts — epoch старта (UTC) или None.
    02.10.2026: distance_m и duration_s — из строки «Duration: 19:45 | Distance: 5.01 km» (None, если нет).

    Имя тренировки COROS кладёт в поле Location; если имени нет, там оказывается
    место («Москва Бег по стадиону») — отбор по маске DD_… делает вызывающий.
    Строка вида 'LabelId: 4800… | SportType: 100' — два значения в одной строке.
    """
    text = _decode(text)
    if not text or "No sport records" in text:
        return []

    out = []
    for block in text.split("\n\n"):
        if "LabelId" not in block:
            continue
        name = label_id = sport_type = rec_date = start_ts = None
        dist_m = dur_s = None
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("Duration:"):
                m_d = re.search(r"Duration:\s*([\d:]+)", line)
                dur_s = _time_sec(m_d.group(1)) if m_d else None
                m_k = re.search(r"Distance:\s*([\d.]+)\s*(km|m)\b", line)
                if m_k:
                    dist_m = float(m_k.group(1)) * (1000 if m_k.group(2) == "km" else 1)
                continue
            # 01.10.2026: дата старта — из заголовка записи «1. Indoor Run — 2026-09-13»,
            # время старта (epoch, UTC) — из «Time Window: startTimestamp=… | endTimestamp=…».
            # Нужно лонгу: в имени DDLong-… даты нет, искать тренировку по дате больше нечем.
            m = re.match(r"\d+\.\s.*?—\s*(\d{4}-\d{2}-\d{2})\s*$", line)
            if m:
                rec_date = m.group(1)
            elif line.startswith("Time Window:"):
                m = re.search(r"startTimestamp=(\d+)", line)
                if m:
                    start_ts = int(m.group(1))
            elif line.startswith("Location:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("LabelId:"):
                for piece in line.split("|"):
                    key, _, value = piece.partition(":")
                    key, value = key.strip(), value.strip()
                    if key == "LabelId":
                        label_id = value
                    elif key == "SportType":
                        sport_type = int(value) if value.isdigit() else None
        if label_id and sport_type is not None:
            out.append({"name": name, "label_id": label_id, "sport_type": sport_type,
                        "date": rec_date, "start_ts": start_ts,
                        "distance_m": dist_m, "duration_s": dur_s})
    return out


RUN_SPORT_TYPES = {100, 101, 102, 103}   # outdoor / indoor / trail / track


def last_run_date_from_records(records) -> str | None:
    """02.10.2026: записи из parse_sport_records → дата последней пробежки или None.
    Пробежка — беговой вид и fitness.is_run (от 3 км или от 20 минут)."""
    import fitness as _fit
    from datetime import datetime as _dt, timezone as _tz
    out = []
    for r in records or []:
        if r.get("sport_type") not in RUN_SPORT_TYPES:
            continue
        if not _fit.is_run(r.get("distance_m"), r.get("duration_s")):
            continue
        d = r.get("date")
        if not d and r.get("start_ts"):
            d = _dt.fromtimestamp(int(r["start_ts"]), tz=_tz.utc).strftime("%Y-%m-%d")
        out.append(d)
    return _fit.latest_date(out)


async def get_vo2max(db_user_id: int) -> float | None:
    """VO2max с часов COROS (новая схема) — для якоря vo2max_device.

    Берёт из сохранённого сырья; если его ещё нет — сходит за ним. Порог сюда не входит
    намеренно: якорь зон у COROS — только VO2max, как решено 17.09.
    """
    import database as db

    row = db.get_raw_service_data(db_user_id, SERVICE)
    raw = json.loads(row["raw_json"]) if row else None
    if not raw:
        raw = await fetch_raw(db_user_id)
    if not raw:
        return None
    value = parse_fitness(raw.get("queryFitnessAssessmentOverview")).get("vo2max")
    return float(value) if value else None


def parse_stress(text) -> dict:
    """Ряд стресса (точки каждые 5 минут). Берём только отметку последней точки —
    до какого момента часы передали данные. Прямого «времени синхронизации» COROS не отдаёт,
    это ближайшее к нему (см. ANCHORS.md, раздел 12)."""
    text = _decode(text)
    if not text:
        return {}
    stamps = [int(x) for x in re.findall(r"timestamp=(\d{9,11})", text)]
    if not stamps:
        return {}
    from datetime import datetime, timezone
    return {"synced_at": datetime.fromtimestamp(max(stamps), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def parse_raw(raw: dict) -> dict:
    """Слой 2: сырьё из raw_service_data → плоский набор полей."""
    raw = raw or {}
    out = {}
    out.update(parse_load(raw.get("queryTrainingLoadAssessment")))
    out.update(parse_fitness(raw.get("queryFitnessAssessmentOverview")))
    out.update(parse_recovery(raw.get("queryRecoveryStatus")))
    out.update(parse_hrv(raw.get("querySleepHrv")))
    out.update(parse_rhr(raw.get("queryRestingHeartRate")))
    out.update(parse_sleep(raw.get("querySleepOverview")))
    out.update(parse_stress(raw.get("queryStressTimeSeries")))
    out.update(parse_activities(raw.get("querySportRecords")))
    return out


# ── Для рекомендации: тот же вид данных, что у остальных сервисов ──

async def _parsed(db_user_id: int, max_age_min: int = 30) -> dict:
    """Разобранные данные с часов: сохранённые, если им меньше max_age_min минут, иначе свежий запрос.
    Рекомендация зовёт нагрузку и восстановление подряд — второй вызов не ходит в сеть."""
    import database as db
    from datetime import datetime, timedelta

    row = db.get_raw_service_data(db_user_id, SERVICE)
    raw = None
    if row:
        try:
            age = datetime.utcnow() - datetime.fromisoformat(str(row["fetched_at"]))
            if age < timedelta(minutes=max_age_min):
                raw = json.loads(row["raw_json"])
        except Exception:
            raw = None
    if raw is None:
        raw = await fetch_raw(db_user_id)
    if raw:
        # Сводка не должна быть старше ответа часов: расчётная готовность (coros-calc)
        # считается при сборке сводки, а в дни без тренировки она сама не пересобирается.
        try:
            row = db.get_raw_service_data(db_user_id, SERVICE)
            uni = db.get_unified_data(db_user_id, max_age_hours=10 ** 6)
            if row and (not uni or str(uni["updated_at"]) < str(row["fetched_at"])):
                from data_normalizer import run_normalization
                run_normalization(db_user_id)
        except Exception as e:
            logger.warning(f"COROS MCP: пересборка сводки не удалась user_id={db_user_id}: {e}")
    return parse_raw(raw) if raw else {}


def _recovery_dict(p: dict) -> dict | None:
    """Восстановление в том же виде, что отдают остальные сервисы."""
    out = {"source": "coros"}
    for src, dst in (("recovery_pct", "recovery_score"), ("recovery_level", "recovery_state"),
                     ("full_recovery", "full_recovery_hours"), ("hrv", "hrv"),
                     ("hrv_baseline", "hrv_baseline"), ("rhr", "rhr"),
                     ("sleep_hours", "sleep_hours"), ("sleep_score", "sleep_score")):
        if p.get(src) is not None:
            out[dst] = p[src]
    if len(out) <= 1:
        return None
    if p.get("synced_at"):
        out["synced_at"] = p["synced_at"]      # как у Garmin: до какого момента есть данные с часов
    return out


async def get_recovery_for_prompt(db_user_id: int) -> dict | None:
    """Восстановление для рекомендации. None — COROS не подключён или данных нет."""
    import database as db
    if not db.get_token(db_user_id, SERVICE):
        return None
    return _recovery_dict(await _parsed(db_user_id))


async def get_full_data(db_user_id: int) -> dict | None:
    """Нагрузка и форма для рекомендации — в том же виде, что у старого COROS."""
    import database as db
    if not db.get_token(db_user_id, SERVICE):
        return None
    p = await _parsed(db_user_id)
    if not p:
        return None
    fitness = {"source": "coros", "summary": "", "total_km": 0, "run_count": 0,
               "avg_pace": "—", "avg_hr": None, "fatigue_level": "unknown"}
    if p.get("vo2max"):
        fitness["vo2max"] = p["vo2max"]
        fitness["vo2max_source"] = "COROS"
    if p.get("threshold_pace_sec"):
        sec = int(p["threshold_pace_sec"])
        fitness["lactate_threshold_pace"] = f"{sec // 60}:{sec % 60:02d}"
    rec = _recovery_dict(p)
    if rec:
        fitness["recovery"] = rec
        score = rec.get("recovery_score")
        if score is not None:
            fitness["fatigue_level"] = "fresh" if score >= 70 else "tired" if score < 40 else "normal"
    ctl, atl = p.get("ctl"), p.get("atl")
    if ctl is not None or atl is not None:
        tsb = round(ctl - atl, 1) if ctl is not None and atl is not None else None
        form = "свежий" if (tsb or 0) > 5 else "перегрузка" if (tsb or 0) < -20 else "небольшая усталость"
        fitness["training_load"] = {
            "source": "coros", "ctl": ctl, "atl": atl, "tsb": tsb, "form_text": form, "trend_text": "",
            "summary": f"CTL={ctl}, ATL={atl}, TSB={tsb} ({form}) [COROS]"}
        if tsb is not None and fitness["fatigue_level"] == "unknown":
            fitness["fatigue_level"] = "fresh" if tsb > 5 else "tired" if tsb < -15 else "normal"
    if p.get("sessions_48h"):
        fitness["load_48h"] = {k: p.get(k) for k in
                               ("sessions_48h", "total_km_48h", "last_activity_hours_ago")}
    parts = []
    if fitness.get("vo2max"):
        parts.append(f"VO2max {fitness['vo2max']}")
    if fitness.get("lactate_threshold_pace"):
        parts.append(f"ЛП {fitness['lactate_threshold_pace']} мин/км")
    if fitness.get("training_load"):
        parts.append(fitness["training_load"]["summary"])
    fitness["summary"] = " | ".join(parts) if parts else "COROS данные получены"
    return fitness
