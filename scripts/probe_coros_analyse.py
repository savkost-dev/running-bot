"""Пробник COROS EvoLab /analyse/query — read-only к нашей БД.

Что делает: GET {base}/analyse/query с accesstoken юзера (как Training Hub).
Печатает структуру ответа и все поля про load/form/fitness/fatigue —
ищем родной COROS Form (TSB) для расчётного TR.

Пишет в базу: НЕТ. Ходит в COROS API (read-only запрос).
Импортирует: database, coros (только _base_url/_headers/_load_token). НЕ импортирует bot.py.

Запуск:
    venv/bin/python3 scripts/probe_coros_analyse.py          # uid=6 (Ксения)
    venv/bin/python3 scripts/probe_coros_analyse.py 17
"""
import sys
import os
import json
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import aiohttp
from coros import _base_url, _headers, _load_token, _TIMEOUT

_PAT = ("load", "form", "tsb", "fatigue", "fitness", "ati", "cti",
        "intensity", "status", "trend", "base")


def _walk(obj, path="", out=None, depth=0):
    if out is None:
        out = []
    if depth > 7:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if any(s in k.lower() for s in _PAT):
                preview = v if isinstance(v, (int, float, str, bool, type(None))) else \
                    (f"[list len={len(v)}]" if isinstance(v, list) else "{dict}")
                out.append((p, preview))
            _walk(v, p, out, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:2]):
            _walk(v, f"{path}[{i}]", out, depth + 1)
    return out


async def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    token = _load_token(uid)
    if not token:
        print(f"user={uid}: нет токена coros")
        return
    base = _base_url(uid)
    print(f"user={uid}  base={base}")

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for params in (None, {"size": 20, "pageNumber": 1}):
            url = f"{base}/analyse/query"
            try:
                async with session.get(url, params=params,
                                       headers=_headers(token)) as resp:
                    raw = await resp.text()
            except Exception as e:
                print(f"\nGET {url} params={params}: ошибка {e}")
                continue
            print(f"\nGET {url} params={params} → {resp.status}, {len(raw)} байт")
            try:
                data = json.loads(raw)
            except Exception:
                print(f"  не-JSON: {raw[:300]}")
                continue
            print(f"  result={data.get('result')} message={data.get('message')}")
            d = data.get("data")
            if isinstance(d, dict):
                print(f"  [data] ключи: {list(d.keys())}")
            elif isinstance(d, list):
                print(f"  [data] list len={len(d)}; ключи [0]: "
                      f"{list(d[0].keys()) if d and isinstance(d[0], dict) else '—'}")
            hits = _walk(d)
            print(f"  Поля load/form/fitness ({len(hits)}):")
            for p, v in hits[:60]:
                print(f"    {p} = {v!r}")
            if data.get("result") == "0000" and d:
                break  # первый успешный вариант достаточен


if __name__ == "__main__":
    asyncio.run(main())
