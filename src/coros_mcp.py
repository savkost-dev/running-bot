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

import aiohttp

import coros_oauth

logger = logging.getLogger(__name__)

SERVICE = coros_oauth.SERVICE          # "coros_mcp"
PROTOCOL_VERSION = "2025-06-18"

# Что забираем: нагрузка (CTL/ATL), форма (VO2max, пороговый темп, прогнозы),
# восстановление. Список тренировок пока не берём — он вторичен.
TOOLS = [
    ("queryTrainingLoadAssessment", {}),
    ("queryFitnessAssessmentOverview", {}),
    ("queryRecoveryStatus", {}),
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


async def _call_tool(session, token: str, name: str, args: dict, req_id: int) -> str | None:
    reply = await _rpc(session, token, "tools/call",
                       {"name": name, "arguments": args}, req_id)
    return _text_of(reply)


async def fetch_raw(db_user_id: int) -> dict | None:
    """Слой 1: сырые ответы COROS MCP as is, БЕЗ парсинга.

    Возвращает {имя_инструмента: текст ответа} и сохраняет в базу.
    None — если доступа нет или всё пришло пустым.
    """
    import database as db

    token = await coros_oauth.ensure_valid_token(db_user_id)
    if not token:
        logger.info(f"COROS MCP fetch_raw: нет токена для user_id={db_user_id}")
        return None

    timeout = aiohttp.ClientTimeout(total=60)
    raw: dict = {}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            init = await _rpc(session, token, "initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "DoDick", "version": "1.0"},
            }, 1)
            if not init:
                logger.error(f"COROS MCP fetch_raw: initialize не прошёл, user_id={db_user_id}")
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

    if not any(v for v in raw.values()):
        logger.info(f"COROS MCP fetch_raw: пусто для user_id={db_user_id}")
        return None

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


def parse_raw(raw: dict) -> dict:
    """Слой 2: сырьё из raw_service_data → плоский набор полей."""
    raw = raw or {}
    out = {}
    out.update(parse_load(raw.get("queryTrainingLoadAssessment")))
    out.update(parse_fitness(raw.get("queryFitnessAssessmentOverview")))
    out.update(parse_recovery(raw.get("queryRecoveryStatus")))
    return out
