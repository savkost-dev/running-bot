# INDEX — вся документация DoDick в одном месте

Начинать любую задачу отсюда. Если файл устарел — так и помечено; исправлять пометку при обновлении файла.

## Проверено
- [DB_SCHEMA.md](DB_SCHEMA.md) — база: где лежит, таблицы и поля, какая функция куда ходит. Пополняется по ходу задач. (с 19.09.2026)
- [CHANGELOG.md](CHANGELOG.md) — история версий.
- [PROCESS_MAP.md](PROCESS_MAP.md) — ⚠️ не обновлялся с 07.08.2026, после этого многое поменялось.
- [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) — ⚠️ не обновлялся с 07.08.2026.

## Есть в папке, содержимое и свежесть не проверены
- [CLAUDE.md](CLAUDE.md)
- [MAP.md](MAP.md)
- [FUNCTIONS.md](FUNCTIONS.md)
- [OPS_CHEATSHEET.md](OPS_CHEATSHEET.md)
- [RECOMMENDATION_ENGINE.md](RECOMMENDATION_ENGINE.md)
- [DATA_NORMALIZER_SPEC.md](DATA_NORMALIZER_SPEC.md)
- [ROADMAP.md](ROADMAP.md)
- [IDEAS_BACKLOG.md](IDEAS_BACKLOG.md)

## Разбор тренировки (/report) — кратко (19.09.2026)
- Пакет данных: `src/ai_package.py`, `build_package`; промт — константа `PROMPT` там же.
- Порядок блоков: СПОРТСМЕН → ЦЕЛЬ И СУТЬ → ГРУППЫ → РЕКОМЕНДАЦИЯ С ВЕЧЕРА → ПЛАН → ФАКТ → САМОЧУВСТВИЕ (утро дня тренировки).
- Посмотреть промт целиком без вызова ИИ: `/report_p 0918` (только админ; то же `/report data`).
