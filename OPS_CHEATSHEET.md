# Справочник: команды и правила работы (DoDick / running-bot)

Обновлено 12.09.2026. Канон по архитектуре — PROCESS_MAP.md и PROJECT_CONTEXT.md; здесь — только «как что делать руками».

## Сервер и деплой
- Сервер: `root@167.172.185.88`, проект `/opt/running-bot`, python только `venv/bin/python` (системный без dotenv).
- SSH: `ssh -i $env:USERPROFILE/.ssh/digitalocean root@167.172.185.88 "<команда>"`
- Копирование файла: `scp -i $env:USERPROFILE/.ssh/digitalocean D:\running-bot\scripts\<файл>.py root@167.172.185.88:/opt/running-bot/scripts/`
- Деплой всего (заливка + перезапуск + git push): `.\deploy.ps1` из `D:\running-bot`. Перед ним: `python -m py_compile D:\running-bot\src\<файл>.py; echo OK`.
- НИКОГДА не перезапускать бота во время живой рассылки (пн/чт/сб 21:00 МСК; бриф пн/чт 19:05).
- Проверка, что правка доехала: `ssh … "grep -c '<маркер>' /opt/running-bot/src/<файл>.py"` — ждём число ≥1. Несколько грепов разделять `;`, не `&&`.
- Лог бота: `ssh … "journalctl -u running-bot --since '10 min ago' --no-pager | grep -E '/report|ошибк' | tail -20"`. `print` попадает в журнал (PYTHONUNBUFFERED=1 в сервисе).

## Кавычки в PowerShell — главное правило
- Внутрь `ssh "…"` нельзя класть команды с кавычками, запятыми и скобками (`python -c`, `sqlite3 "SELECT …"`): PowerShell ломает разбор.
- Решение: всегда писать скрипт в `scripts/<имя>.py` (докстринг: зачем и как запускать), заливать `scp`, запускать по ssh. Скрипты не импортируют bot.py; путь к базе и функции — через `import database as db` (`sys.path.insert(0, …/src)`).
- Русский текст в выводе PowerShell искажается — перед чтением кода: `[Console]::OutputEncoding=[Text.Encoding]::UTF8`.
- Показать строки файла: `(Get-Content D:\running-bot\src\bot.py)[100..120]` (индексы с нуля: строка N = индекс N-1).
- Найти: `Select-String -Path D:\running-bot\src\*.py -Pattern 'слово' | Select-Object Filename, LineNumber, Line`.
- Имя функции, в которой лежит строка N: `$d = Select-String -Path D:\running-bot\src\bot.py -Pattern '^(async )?def '; ($d | ? { $_.LineNumber -lt N } | select -Last 1).Line`

## Правила правок
- Маленькие задачи по одной; своё — только как «предлагаю: …» и после «да».
- Перед правкой bot.py — dryRun; переименования — с предупреждением; новые функции отдельно, не раздувать существующие.
- MCP edit_file меняет ОДНО вхождение; якорить по уникальным соседним строкам.
- Сводка изменений по сути → потом команды; версию не бампать без команды.

## Полезные админ-команды бота
- `/report 0911` = `/report DD_20260911` (год текущий); `/report data` — сырой пакет для ИИ.
- `/report_user` — разбор выбранного пользователя.
- `/mailing [YYYY-MM-DD]` — отчёт по рассылке (история дат — только с 12.09, таблица recommendation_history).
- `/shadow_run <дата> [режим] [тип] [limit]` — теневой прогон Шага 2, ничего не шлёт:
  дата `0911`/`20260911`; режим `smart|deep|fast`; тип = `run_kind` в истории (`shadow`, повтор — `shadow_1`); limit — первые N человек для пробы.
- `/stats`, `/help` — сводка и полный список.

## Скрипты на сервере (все: `cd /opt/running-bot && venv/bin/python scripts/<имя>.py …`)
- `probe_chart_model.py <uid> <strava|garmin|coros> [селектор]` — круги, план, секундный ряд, сплиты по 200/100 м одного источника.
- `probe_splits200.py <uid> [селектор]` — нарезка Garmin по кругам.
- `probe_garmin_stream.py <uid>` — частота точек Garmin details.
- `probe_analysis_blocks.py <дата> [группа]` — структура анализа Шага 1 и темпы группы.
- `rebuild_templates.py <YYYY-MM-DD>` — пересобрать эталоны (workout_templates) за дату исправленным генератором.
- `probe_ai_modes.py` — распределение режимов ИИ среди пользователей с зонами.
- `set_calc_to_smart.py` — перевод calc → smart (разово, повтор безопасен).
- uid Антона в базе = 2.

## База (SQLite, /opt/running-bot/data/running_bot.db)
- `last_recommendation` — одна строка на пользователя (последняя). История — `recommendation_history` (ключ user + дата + run_kind: mailing / manual / shadow…; поля workout_type, ai_mode, advice_json целиком).
- Запросы к базе — только скриптом (см. кавычки), не через `sqlite3` в ssh-строке.
