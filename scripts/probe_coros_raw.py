"""Пробник сырья COROS — read-only.

Печатает структуру сохранённого raw_service_data(coros): верхние ключи,
ключи dashboard, и РЕКУРСИВНО ищет всё похожее на сон/пробуждение
(sleep, wake, bed, rest, hrv, recovery) с путями и значениями.
Отвечает на вопрос: есть ли у COROS данные сна в нашем сырье вообще.

Пишет в базу: НЕТ (read-only).
Импортирует: database. НЕ импортирует bot.py.

Запуск:
    venv/bin/python3 scripts/probe_coros_raw.py            # uid=6 (Ксения)
    venv/bin/python3 scripts/probe_coros_raw.py 17         # другой db_user_id
"""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import database as db

_PAT = ("sleep", "wake", "bed", "rest", "hrv", "recovery", "night",
        "ati", "cti", "load", "form", "tsb", "fatigue", "fitness", "intensity",
        "sync", "upload", "updatetime", "lasttime", "timestamp")


def _walk(obj, path="", out=None, depth=0):
    """Рекурсивно собирает пути, где ключ содержит паттерн сна."""
    if out is None:
        out = []
    if depth > 6:
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
        for i, v in enumerate(obj[:3]):  # первые 3 элемента списков
            _walk(v, f"{path}[{i}]", out, depth + 1)
    return out


def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 6

    row = db.get_raw_service_data(uid, "coros")
    if not row:
        print(f"user={uid}: нет сырья coros")
        return
    print(f"user={uid}  fetched_at={row['fetched_at']}  (UTC; +3 = МСК)")
    g = json.loads(row["raw_json"])

    print(f"\nверхние ключи сырья: {list(g.keys())}")
    dash = g.get("dashboard")
    if isinstance(dash, dict):
        print(f"[dashboard] ключи: {list(dash.keys())}")
        data = dash.get("data")
        if isinstance(data, dict):
            print(f"[dashboard.data] ключи: {list(data.keys())}")
            si = data.get("summaryInfo")
            if isinstance(si, dict):
                print(f"[dashboard.data.summaryInfo] ВСЕ ключи: {list(si.keys())}")

    hits = _walk(g)
    print(f"\nПоля про сон/HRV/нагрузку/время синка ({len(hits)}):")
    for p, v in hits:
        print(f"  {p} = {v!r}")
    if not hits:
        print("  ничего не найдено")

    # timestamp сегодняшней записи dayList из analyse — кандидат на время синка
    an = g.get("analyse")
    if isinstance(an, dict):
        from datetime import datetime, timezone, timedelta
        today = datetime.now(timezone(timedelta(hours=3))).strftime("%Y%m%d")
        for it in ((an.get("data") or {}).get("dayList") or []):
            if isinstance(it, dict) and str(it.get("happenDay")) == today:
                ts = it.get("timestamp")
                human = ""
                try:
                    human = datetime.fromtimestamp(int(ts) / (1000 if int(ts) > 10**11 else 1),
                                                   timezone.utc).isoformat()
                except Exception:
                    pass
                print(f"\ndayList[сегодня {today}]: timestamp={ts!r} (UTC: {human})")
                break


if __name__ == "__main__":
    main()
