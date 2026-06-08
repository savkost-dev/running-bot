# Карта зависимостей и дублей

> 2026-06-03 — первая версия (чистая документация, код не тронут)
> 2026-06-08 — обновлено: вынесены `recovery.py` и `fitness.py` из bot.py; актуализированы размеры

---

## Граф зависимостей (кто что импортирует)

```
bot.py
 ├── weather          (get_weather_for_workout, format_weather_*)
 ├── database         (все функции хранения)
 ├── strava           (ensure_valid_token, get_training_load, ...)
 ├── telegram_reader  (find_next_workout, find_next_long_run, ...)
 ├── claude_advisor   (analyze_workout, recommend_group, format_*, ...)
 ├── zones            (get_pace_zones, recalculate_and_save)
 ├── recovery         (_get_recovery_data, _get_unified_recovery, _recovery_scenario, ...)  ← вынесено 2026-06-08
 ├── fitness          (get_*_fitness_data, refresh_athlete_cache, _get_vo2max_from_tracker)  ← вынесено 2026-06-08
 └── [ленивые импорты внутри функций]
      ├── garmin       (get_garmin_fitness_data, upload_workout, ...)
      ├── coros        (get_coros_fitness_data, ...)
      ├── polar        (get_polar_fitness_data, ...)
      ├── whoop        (get_full_recovery_data, ...)
      └── fit_generator (build_garmin_interval_workout, ...)

recovery.py  ← новый модуль (домен восстановления, вынесен из bot.py)
 ├── database         (get_preferences, get_token, get/save_garmin_recovery_cache)
 └── [ленивые импорты] garmin, whoop, coros, polar, data_normalizer, database

fitness.py   ← новый модуль (домен формы + кэш атлета, вынесен из bot.py)
 ├── database         (get_token, save_athlete_cache, get_athlete_cache)
 ├── strava           (get_full_athlete_data)
 └── [ленивые импорты] strava, garmin, coros, polar

oauth_server.py
 ├── database         (get_token, save_token, ...)
 ├── strava           (exchange_code, refresh_access_token)
 ├── whoop            (exchange_code, refresh_access_token)
 └── polar            (exchange_code, register_user, refresh_access_token)

Изолированные модули (ничего проектного не импортируют):
 claude_advisor.py   — только openai SDK + dotenv
 database.py         — только sqlite3 + cryptography
 telegram_reader.py  — только telethon
 zones.py            — только math
 data_normalizer.py  — только dataclasses + math
 weather.py          — только aiohttp
 fit_generator.py    — только struct/io
 garmin.py           — только garminconnect + database
 coros.py            — только httpx + database
 polar.py            — только httpx + database
 strava.py           — только aiohttp + database
 whoop.py            — только aiohttp + database
```

**Вывод:** `bot.py` — единственный оркестратор. `recovery.py` и `fitness.py` — чистые
листья (тянут только из database/сервисов, обратных импортов в bot.py нет, циклов нет).
`claude_advisor.py` при разбивке потребует фасадной стратегии (см. ниже): bot.py
обращается к нему через атрибут модуля (`claude_advisor.X`), в т.ч. к приватным членам
и мутабельному глобалу `last_prompt`.

---

## Размеры файлов (2026-06-08)

| Файл | Строк | Статус |
|------|-------|--------|
| `bot.py` | 4724 | ⚠️ громоздко — 118 функций; recovery/fitness уже вынесены, далее keyboards/recommendation/schedulers |
| `claude_advisor.py` | 3110 | ⚠️ громоздко — 45 функций, промты + математика + форматтеры (разбивка не начата) |
| `database.py` | 1385 | ок — один слой, единая ответственность |
| `data_normalizer.py` | 1026 | ок |
| `telegram_reader.py` | 762 | ок |
| `coros.py` | 679 | ок |
| `garmin.py` | 649 | ок |
| `polar.py` | 605 | ок |
| `strava.py` | 550 | ок |
| `fit_generator.py` | 481 | ок |
| `oauth_server.py` | 434 | ок |
| `recovery.py` | 359 | ✅ вынесен из bot.py (6 функций, чистый лист) |
| `whoop.py` | 310 | ок |
| `weather.py` | 246 | ок |
| `zones.py` | 234 | ок |
| `fitness.py` | 207 | ✅ вынесен из bot.py (6 функций, чистый лист) |
| остальные | <10 | ок |

> История bot.py: 5237 (исх.) → 4901 (−recovery) → 4724 (−fitness).

---

## Дубли функций

### Настоящие дубли — одна и та же логика в разных файлах

| Функция | Файлы | Различия |
|---------|-------|---------|
| `_pace_to_sec` | `claude_advisor.py`, `data_normalizer.py` | data_normalizer: robustная (None при ошибке, поддерживает точку как разделитель); claude_advisor: упрощённая (падает на невалидном вводе) |
| `_sec_to_pace` | `claude_advisor.py`, `data_normalizer.py` | data_normalizer: None-safe; claude_advisor: без проверок |
| `_zones_from_vdot` | `zones.py`, `data_normalizer.py` | Одинаковый алгоритм Дэниелса, но разные реализации: zones.py использует общие константы `_ZONE_FRACTIONS`, data_normalizer дублирует формулу с захардкоженными числами |

**Что делать:** вынести `_pace_to_sec` / `_sec_to_pace` в `zones.py` или отдельный `utils.py`.
`data_normalizer._zones_from_vdot` заменить вызовом `zones._zones_from_vdot`.

### Интерфейсные дубли — одно имя, разные реализации (это норма)

Сервисы (garmin, coros, polar, strava, whoop) реализуют единый интерфейс:

| Функция | Файлы | Комментарий |
|---------|-------|-------------|
| `get_auth_url` | strava, whoop, polar | OAuth шаг 1 — разные URL |
| `exchange_code` | strava, whoop, polar | OAuth шаг 2 — разные эндпоинты |
| `refresh_access_token` | strava, whoop, polar | Обновление токена |
| `ensure_valid_token` | strava, whoop | Проверяет срок + обновляет |
| `get_full_data` | garmin, coros, polar | Все данные для промта |
| `get_training_load` | garmin, coros, strava | CTL/ATL/TSB или аналог |
| `get_training_status` | garmin, coros | Статус тренировок |
| `get_hrv_status` | garmin, coros | HRV статус |
| `get_recovery` | whoop, coros | Данные восстановления |
| `get_recovery_for_prompt` | coros, polar | Recovery-dict для `_get_recovery_data()` в bot.py |
| `get_sleep` | whoop, polar | Данные сна |
| `get_vo2max` | garmin, coros, polar | VO2max из сервиса |
| `connect` | garmin, coros | Инициализация клиента |
| `_load_token` | garmin, coros, polar | Читает access_token из БД |
| `_save_token` | garmin, coros | Сохраняет токены в БД |
| `_headers` | coros, polar | HTTP-заголовки с авторизацией |
| `_get` | coros, polar | GET с автообновлением токена |

**Вывод:** интерфейсные дубли — намеренный полиморфизм, трогать не нужно.
`ensure_valid_token` отсутствует в polar и coros — стоит добавить для единообразия.

---

## План разбивки

### bot.py → модули (порядок: recovery → fitness → keyboards → recommendation → schedulers)

| Новый файл | Что переносить | Статус |
|------------|---------------|--------|
| `recovery.py` | `_get_recovery_data`, `_get_unified_recovery`, `_recovery_scenario`, `_fetch_garmin_recovery`, `_update_garmin_recovery_from_raw`, `_garmin_observation_end` | ✅ сделано (v0.24.67) |
| `fitness.py` | `get_*_fitness_data`, `refresh_athlete_cache`, `_get_vo2max_from_tracker` | ✅ сделано (v0.24.68) |
| `keyboards.py` | все `_build_*_keyboard`, `_build_*_text`, `_settings_nav`, `_svc_*`, `_merge_keyboards`, `get_main_keyboard` | ⏳ след. (билдеры перемешаны с `cmd_*` — расцепить) |
| `recommendation.py` | `_send_recommendation`, `_send_ai_variant_b`, `_send_workout/morning/long_run_recommendation`, `_build_variant_b_prompt`, `_user_has_data` | ⏳ |
| `schedulers.py` | `scheduled_*` (вкл. `scheduled_wakeup_poll`) + ночные хелперы (`_night_ready`, `_collect_morning_snapshot`, `_sync_night_services`, `_normalize_after_catch`) + `_autoanalyze_post` + `_notify_all` + `_broadcast_split` + `_edit_newer` | ⏳ |

### claude_advisor.py → фасад + 5 подмодулей (разбивка не начата)

⚠️ **Фасадная стратегия обязательна**: bot.py обращается к `claude_advisor.X` через атрибут
модуля, в т.ч. к приватным (`_SPEC_LABELS`, `_sanitize_group_name`, `_recovery_value`,
`_recovery_descriptor`, `_build_step2_prompt`, `_build_analyze_prompt`) и к мутабельному
глобалу `last_prompt` (пишется в `ask_groq`, читается в bot.py:604). Поэтому
`claude_advisor.py` остаётся **тонким фасадом-реэкспортом**, код переезжает в подмодули,
`last_prompt` проксируется через module-level `__getattr__` (PEP 562).

| Новый файл | Что переносить | ~строк | Зависит от |
|------------|---------------|--------|-----------|
| `pace_utils.py` | pace-конверсии + зональные константы/математика (`_pace_sec`, `_classify_zone`, `_intensity`, `_time_to_termination`, `_zone_*`, `_workout_character`, `_SPEC_LABELS`, `_TTT_*`) | ~250 | — (лист, выносить первым) |
| `prompts.py` | все `build_*_prompt`, `_build_recovery_block`, `_estimate_vo2max` | ~750 | pace_utils |
| `recommend.py` | `recommend_group`, `recommend_long`, движок | ~400 | pace_utils |
| `formatting.py` | `format_*_message`, `recommendation_to_*_advice`, `_pct_bar`, `_suit_comment`, `_sanitize_group_name`, `_recovery_value/descriptor` | ~750 | pace_utils |
| `ai_client.py` | `_get_client`, `ask_groq`, `analyze_workout`, `ask_deepseek_garmin`, `generate_*`, `last_prompt` | ~450 | prompts |
| `claude_advisor.py` (фасад) | реэкспорт всего + `__getattr__` для `last_prompt` | ~60 | все выше |

### utils.py — утилиты без домашнего файла

Вынести единожды, всем импортировать:
- `_pace_to_sec` (robustная версия из data_normalizer)
- `_sec_to_pace` (robustная версия из data_normalizer)
- Убрать дубли из claude_advisor и data_normalizer
