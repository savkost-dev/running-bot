# DB_SCHEMA — база данных DoDick

Файл пополняется по ходу задач: только проверенные факты (кодом или запросом к базе), с датой.
Полная схема всех таблиц — выводом `scripts/schema.py` (локально, таблицы + CREATE).

## Где лежит база
- Боевая, на сервере: `/opt/running-bot/running_bot.db` (НЕ `data/`). (19.09.2026)
- Локальная копия: `D:\running-bot\data\running_bot.db` — обновляется с сервера вручную, может отставать на дни. (19.09.2026)
- Скрипты-разведчики берут путь к базе аргументом: `scripts/probe_history_tables.py`, `scripts/probe_mornings.py`, `scripts/probe_reco_kinds.py`.

## recommendation_history — история рекомендаций Шага 2 (19.09.2026)
- Одна строка на (user_id, workout_date, run_kind); повтор с тем же ключом — замена.
- `workout_date` — строка `ГГГГ-ММ-ДД`.
- `run_kind` — каким прогоном сделана запись:
  - `mailing` — боевая вечерняя рассылка (то, что ушло пользователю);
  - `shadow`, `shadow_v2…v5`, `shadow_fast`, `shadow_deep`, `shadow_1/2` — теневые прогоны, пользователю не шлются;
  - `manual` — значение по умолчанию в коде, в базе таких записей нет.
- `recommended_group` — основная рекомендация (номер группы строкой, напр. `3.5`).
- `groups_pct` — JSON «группа → %» по всем группам, напр. `{"1": 5, "2": 20, "3": 70, "3.5": 90, ...}`.
- `advice_json` — полный ответ ИИ целиком; новое поле в JSON-ответе Шага 2 сохранится здесь без правок базы.
- Прочие поля: workout_type, recommended_pace, reason, if_feeling_good, if_tired, workout_title, groups_raw, extra_groups_raw, ai_mode, evening_recovery_score, lowered_by_recovery, saved_at.
- Пишет: `database.save_recommendation_history`. Читает: `database.get_recommendations_for_date(date)` — только `mailing`, работает на любые прошлые даты; её используют /mailing и пакет разбора (/report).

## last_recommendation (19.09.2026)
- Одна строка на пользователя, перезаписывается следующей рекомендацией любого происхождения — для прошлых дат не годится. /mailing с 19.09 её больше не читает.

## mornings — утренние снимки по дням (19.09.2026)
- Одна строка на (user_id, date); колонки: user_id, date, sources, morning_caught, morning_tr, morning_bb, morning_hrv, morning_rhr, morning_sleep_h, morning_wake_at, morning_snapshot_at.
- Читает: `database.get_morning_caught(user_id, day)` — с `day` снимок за этот день; разбор берёт утро ДНЯ тренировки.

## unified_cache (19.09.2026)
- Одна строка на пользователя; поля `morning_*` — текущий утренний снимок, каждое утро перезаписываются (истории нет).
- Читает: `database.get_morning_caught(user_id)` без `day`.

## workout_analysis — анализ анонса, Шаг 1 (19.09.2026)
- Поле `analyzed_json` — анализ целиком: summary, overall_purpose, what_to_watch, workout_type (грубый: interval/long…), `groups` (темпы групп по блокам), `structure` (блоки, «направленность»), `modes`.
- Разбор читает запись за дату (`ai_package._s4_by_date`, is_valid = 1).
- Блок ГРУППЫ текстом: `claude_advisor.build_groups_text(analysis)`.

## recommendation_ratings — оценки рекомендаций и разборов (19.09.2026)
- Колонки: id, user_id, workout_date, rating (1–10), ai_mode, comment, created_at, kind.
- `kind` (с 19.09): `recommendation` (по умолчанию, все старые записи) или `report` — оценка разбора.
- Пишет: `database.save_rating(..., kind=)`. Кнопка «⭐ Оценить разбор» несёт объект в себе: `rate_show:report:<ГГГГММДД>:<режим>`.
- История оценок = эта таблица; тексты разборов не хранятся. Средняя в /stats — только по рекомендациям.

## Прочие таблицы с датами (есть, не разбирались)
athlete_cache, garmin_recovery_cache, raw_service_data.
