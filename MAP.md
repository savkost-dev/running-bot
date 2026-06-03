# Карта зависимостей и дублей

> 2026-06-03 — чистая документация, код не тронут

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
 └── [ленивые импорты внутри функций]
      ├── garmin       (get_garmin_fitness_data, upload_workout, ...)
      ├── coros        (get_coros_fitness_data, ...)
      ├── polar        (get_polar_fitness_data, ...)
      ├── whoop        (get_full_recovery_data, ...)
      └── fit_generator (build_garmin_interval_workout, ...)

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

**Вывод:** `bot.py` — единственный оркестратор, всё остальное — изолированные слои.
`claude_advisor.py` при разбивке не создаст проблем с импортами.

---

## Размеры файлов

| Файл | Строк | Статус |
|------|-------|--------|
| `bot.py` | 3308 | ⚠️ громоздко — 93 функции, всё в одном |
| `claude_advisor.py` | 2684 | ⚠️ громоздко — 48 функций, промты + математика + форматтеры |
| `database.py` | 1062 | ок — один слой, единая ответственность |
| `telegram_reader.py` | 631 | ок |
| `data_normalizer.py` | 377 | ок |
| `coros.py` | 455 | ок |
| `polar.py` | 413 | ок |
| `garmin.py` | 409 | ок |
| `strava.py` | 383 | ок |
| `oauth_server.py` | 363 | ок |
| `fit_generator.py` | 350 | ок |
| остальные | <220 | ок |

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

## Предложения по разбивке (без изменения импортов)

### bot.py → 4 модуля

| Новый файл | Что переносить | Строк |
|------------|---------------|-------|
| `keyboards.py` | все `_build_*_keyboard`, `_build_*_text`, `_settings_nav`, `_svc_*`, `_merge_keyboards`, `get_main_keyboard` | ~350 |
| `commands_admin.py` | все `cmd_stats/users/services/debug*/ratings/feedbacks/analyze/reanalyze/show_analyze/test_*/preprocess_mode/prompt` + вспомогательные `_run_*`, `_collect_*`, `_reanalyze_one`, `_format_analysis_result` | ~500 |
| `schedulers.py` | `scheduled_*` (5 штук) + `_autoanalyze_post` + `_edit_newer` + `_notify_all` + `_broadcast_split` | ~400 |
| `bot.py` (остаток) | команды пользователя, логика рекомендаций, обработчики, main | ~1600 |

### claude_advisor.py → 3 модуля

| Новый файл | Что переносить | Строк |
|------------|---------------|-------|
| `recommendation.py` | `recommend_group`, `recommend_long` + вся математика (`_intensity`, `_time_to_termination`, `_zone_*`, `_form_score`, `_recovery_value`, `_group_primary_pace`, `_spec_component`, `_quality_label`, `_workout_character`, `_pace_sec`, `_classify_zone`) | ~400 |
| `formatters.py` | `format_evening_message`, `format_morning_message`, `format_long_run_message`, `recommendation_to_advice`, `recommendation_to_long_advice`, `generate_step2_prose`, `_pct_bar`, `_suit_comment`, `_shorten_group_label`, `_sanitize_group_name`, `_recovery_warning`, `_recovery_descriptor`, `_sec_to_pace`, `_pace_to_sec`, `_add_sec_to_pace` | ~600 |
| `claude_advisor.py` (остаток) | промпты, API-вызовы (analyze_workout, ask_groq, ask_deepseek_garmin, generate_ai_b_*) | ~1500 |

### utils.py — утилиты без домашнего файла

Вынести единожды, всем импортировать:
- `_pace_to_sec` (robustная версия из data_normalizer)
- `_sec_to_pace` (robustная версия из data_normalizer)
- Убрать дубли из claude_advisor и data_normalizer
