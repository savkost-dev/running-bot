"""Проверка: чем разбирается FIT-файл COROS (fit_tool падает на файлах COROS, 18.09.2026).

Скачивает FIT последней DD-активности пользователя через COROS MCP, сохраняет в /tmp
и пробует разобрать тремя библиотеками: fit_tool, fitdecode, fitparse (если установлены).
Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/probe_coros_fit.py <db_user_id> [селектор]
Ничего не меняет.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import ai_package as ap  # noqa: E402
import coros_mcp  # noqa: E402


async def main():
    uid = int(sys.argv[1])
    sel = ap._expand_selector(sys.argv[2] if len(sys.argv) > 2 else None)
    import re
    records = coros_mcp.parse_sport_records(await coros_mcp.fetch_sport_records(uid, days=30))
    runs = [r for r in records if re.search(r"DD[-_]", str(r.get("name") or ""))]
    rec = (next((r for r in runs if ap._name_matches(sel, r.get("name"))), None) if sel
           else (runs[0] if runs else None))
    if not rec:
        sys.exit("активность не найдена: " + ", ".join(str(r.get("name")) for r in runs[:5]))
    print("активность:", rec.get("name"), rec["label_id"])
    data = await coros_mcp.fetch_fit_bytes(uid, rec["label_id"], rec["sport_type"])
    if not data:
        sys.exit("FIT не скачался")
    path = "/tmp/coros_probe.fit"
    open(path, "wb").write(data)
    print("файл:", path, len(data), "байт")

    try:
        from fit_tool.fit_file import FitFile
        from fit_tool.profile.messages.record_message import RecordMessage
        n = sum(1 for r in FitFile.from_bytes(data).records if isinstance(r.message, RecordMessage))
        print("fit_tool: OK, record-сообщений", n)
    except Exception as e:  # noqa: BLE001
        print("fit_tool: ОШИБКА", type(e).__name__, str(e)[:120])

    try:
        import fitdecode
        n = 0
        with fitdecode.FitReader(path) as fit:
            for frame in fit:
                if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "record":
                    n += 1
        print("fitdecode: OK, record-сообщений", n)
    except ImportError:
        print("fitdecode: не установлен")
    except Exception as e:  # noqa: BLE001
        print("fitdecode: ОШИБКА", type(e).__name__, str(e)[:120])

    try:
        from fitparse import FitFile as FP
        n = sum(1 for _ in FP(path).get_messages("record"))
        print("fitparse: OK, record-сообщений", n)
    except ImportError:
        print("fitparse: не установлен")
    except Exception as e:  # noqa: BLE001
        print("fitparse: ОШИБКА", type(e).__name__, str(e)[:120])


asyncio.run(main())
