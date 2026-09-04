"""
COROS OAuth 2.0 (MCP) — подключение аккаунта COROS без логина и пароля.

Пользователь жмёт кнопку → уходит на страницу входа COROS → возвращается
на /coros/callback с кодом → меняем код на токен.

Клиент публичный (COROS выдал его без секрета), поэтому защита строится
на PKCE: перед уходом генерируем случайную строку, её хэш кладём в ссылку,
а саму строку предъявляем при обмене кода. Строка живёт в памяти процесса
до возврата — бот и веб-сервер работают в одном процессе, база не нужна.

Сервис в базе называется "coros_mcp", чтобы не пересекаться со старым
подключением по паролю ("coros").
"""
import base64
import hashlib
import logging
import os
import secrets
import time as _time

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SERVICE = "coros_mcp"

# Европейский узел COROS MCP (адреса взяты из их oauth-authorization-server)
BASE = os.getenv("COROS_MCP_BASE", "https://mcpeu.coros.com")
AUTHORIZE_URL = f"{BASE}/oauth2/authorize"
TOKEN_URL = f"{BASE}/oauth2/token"
REVOKE_URL = f"{BASE}/oauth2/revoke"
MCP_URL = f"{BASE}/mcp"

CLIENT_ID = os.getenv("COROS_MCP_CLIENT_ID", "1fd33a76-1aa5-4b67-9e76-ae20526f3f30")
REDIRECT_URI = os.getenv("COROS_MCP_REDIRECT_URI", "https://api.dodick.run/coros/callback")
SCOPE = "openid mcp.tools offline_access"

# Незавершённые подключения: state -> (telegram_id, code_verifier, время создания)
_PENDING: dict = {}
_PENDING_TTL = 900  # 15 минут на то, чтобы залогиниться


def _b64url(raw: bytes) -> str:
    """Кодирование без символов, ломающих URL, и без хвостовых знаков '='."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _drop_expired() -> None:
    """Чистка забытых попыток подключения, чтобы словарь не рос."""
    now = _time.time()
    for key in [k for k, v in _PENDING.items() if now - v[2] > _PENDING_TTL]:
        _PENDING.pop(key, None)


def build_auth_url(telegram_id: int) -> str:
    """Ссылка на страницу входа COROS для конкретного пользователя."""
    from urllib.parse import urlencode

    _drop_expired()
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(24))
    _PENDING[state] = (telegram_id, verifier, _time.time())

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def take_pending(state: str):
    """Достать и погасить незавершённое подключение. Возвращает (telegram_id, verifier)."""
    _drop_expired()
    item = _PENDING.pop(state, None)
    if not item:
        return None
    return item[0], item[1]


async def _post_token(data: dict) -> dict:
    """Общий запрос к COROS за токеном. Клиент публичный, секрет не передаём."""
    data = dict(data)
    data["client_id"] = CLIENT_ID
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(TOKEN_URL, data=data) as resp:
            text = await resp.text()
            if resp.status != 200:
                logger.error(f"COROS token HTTP {resp.status}: {text[:300]}")
                raise RuntimeError(f"COROS token HTTP {resp.status}")
            import json as _json
            return _json.loads(text)


async def exchange_code(code: str, verifier: str) -> dict:
    """Обмен кода авторизации на токен доступа."""
    return await _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })


async def refresh_access_token(refresh_token: str) -> dict:
    """Продление доступа без участия пользователя."""
    return await _post_token({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })


def save_tokens(db_user_id: int, token_data: dict) -> None:
    """Сохранение токена в базу под именем сервиса coros_mcp."""
    from database import save_token

    expires_at = int(_time.time()) + int(token_data.get("expires_in", 3600))
    save_token(
        db_user_id,
        SERVICE,
        token_data["access_token"],
        token_data.get("refresh_token"),
        str(expires_at),
    )


async def ensure_valid_token(db_user_id: int):
    """Действующий токен пользователя: при необходимости продлевает сам."""
    from database import get_token

    row = get_token(db_user_id, SERVICE)
    if not row:
        return None
    access = row.get("access_token")
    refresh = row.get("refresh_token")
    expires_at = row.get("expires_at")
    if not access:
        return None
    try:
        expired = int(float(expires_at or 0)) - 120 <= int(_time.time())
    except (TypeError, ValueError):
        expired = True
    if not expired:
        return access
    if not refresh:
        return None
    try:
        fresh = await refresh_access_token(refresh)
    except Exception as e:
        logger.error(f"COROS refresh error uid={db_user_id}: {e}")
        return None
    save_tokens(db_user_id, fresh)
    return fresh["access_token"]
