# DoDick Bot — Контекст проекта

## Что это
Telegram бот @DoDick_bot для бегового клуба Dusty Dumbbells.
Помогает участникам выбрать группу для тренировки на основе их физической формы.

## Инфраструктура
- Сервер: DigitalOcean 167.172.185.88 (2GB RAM, Ubuntu 24.04)
- SSH: C:\Users\savko\.ssh\digitalocean
- Проект локально: D:\running-bot\
- GitHub: github.com/savkost-dev/running-bot (ветка master)
- Деплой: powershell D:\running-bot\deploy.ps1

## Стек
- Python 3.12, python-telegram-bot, Telethon
- DeepSeek API (3 режима: deep/smart/fast)
- SQLite база данных
- Сервисы: Strava, Garmin, COROS, Whoop, Polar

## Структура проекта
```
src/
  bot.py            — главный файл, все команды и хендлеры
  claude_advisor.py — промпты и вызовы DeepSeek
  telegram_reader.py — чтение канала @Dusty_Dumbbells через Telethon
  strava.py         — Strava OAuth + данные
  garmin.py         — Garmin Connect (логин/пароль)
  coros.py          — COROS (неофициальный API)
  polar.py          — Polar AccessLink OAuth
  whoop.py          — Whoop OAuth
  weather.py        — OpenWeatherMap
  fit_generator.py  — генерация .fit файлов для Garmin
  oauth_server.py   — веб-сервер для OAuth callback (порт 8080)
  database.py       — SQLite, все таблицы
  version.py        — версионность
```

## Расписание тренировок клуба
- Вторник/Пятница: интервальные (отрезки по дистанции или времени)
- Воскресенье: Long Run 100 минут по темповым группам
- Анонс выходит НАКАНУНЕ утром в @Dusty_Dumbbells
- Комментарии к посту: доп группы 3.5, 5 появляются в течение дня

## Формат анонса тренировки
```
Начало:     "Вторник 03.06" или "Воскресенье 01.06"
Локация:    в backticks или ссылка Яндекс.Карты
Расписание: "7:00 – сбор / 7:10 – старт"
Разминка:   "4 км в темпе ~6:00 мин/км"
Работа:     "12 по 500/300м" (описание задания)
Группы:     эмодзи 1️⃣-7️⃣ с темпом или временем на отрезке
Заминка + Объём
```

Доп. группы (3.5, 5) появляются в комментариях к посту — бот читает их через
Telethon и добавляет в `extra_groups`. Шумовые строки (анонсы забегов, личные
советы, даты ДД.ММ) фильтруются в `_filter_extra_group_comment()`.

## Что берём из каждого сервиса
| Сервис  | Данные |
|---------|--------|
| Strava  | CTL/ATL/TSB (нагрузка), активности 48ч, suffer_score |
| Garmin  | VO2max, Training Readiness, Body Battery, HRV, Training Status, лактатный порог |
| COROS   | VO2max, Training Load, HRV |
| Polar   | VO2max, Training Load, сон |
| Whoop   | Recovery Score, HRV, качество сна |

## Логика рекомендации
1. Находим анонс тренировки в канале (`find_next_workout` / `find_next_long_run`)
2. Берём данные пользователя (трекеры + профиль)
3. Отправляем в DeepSeek → получаем группу с % подходимости
4. Показываем шкалу групп + стратегию (для Long Run — прогрессия или ровный темп)
5. Предлагаем скачать .fit файл или загрузить в Garmin Connect

## AI-модели
| Режим | Модель | Время |
|-------|--------|-------|
| deep  | deepseek-v4-pro (thinking) | ~2-3 мин |
| smart | deepseek-v4-flash (thinking) | ~30-60 сек |
| fast  | deepseek-v4-flash | ~10 сек |

Fallback при ошибке DeepSeek: Groq `llama-3.3-70b-versatile`.
Прод-режим автоанализа анонсов (preprocess_mode) — сейчас `deep`.

## Ключевые данные пользователя в промпте
- VO2max (из трекера или вручную)
- Лактатный порог (темп + пульс)
- CTL/ATL/TSB или Training Load
- Recovery Score / Training Readiness
- Нагрузка за последние 48ч (suffer_score)
- Погода на время тренировки (OpenWeatherMap)

## Разделение рассылки
Рассылки `scheduled_evening` и `scheduled_new_workout_check` делятся на два потока:

**has_data = True** (есть VO2max в профиле ИЛИ хотя бы один токен трекера):
→ Полная рекомендация через DeepSeek — группа, % подходимости, стратегия

**has_data = False** (нет ни профиля, ни трекера):
→ Упрощённое уведомление: дата, локация, расписание, список групп + призыв заполнить профиль

`scheduled_morning` (утренняя) — только пользователи с данными, остальных не беспокоит.

## Мягкая деактивация
- `Forbidden` при отправке → `is_active=0` + `deactivated_at` в `user_preferences`
- Любое входящее сообщение от пользователя → `is_active=1` (автовосстановление)
- Рассылки используют `get_active_users()` — неактивные не получают сообщения

## Таблицы БД
```
users                — telegram_id, name, username, created_at
user_tokens          — oauth токены (Strava, Garmin, COROS, Polar, Whoop)
user_preferences     — настройки: ai_mode, уведомления, is_active, deactivated_at
user_profile         — VO2max, лактатный порог, пол, источники данных, specialization
athlete_cache        — CTL/ATL/TSB, прогнозы Риегеля, последнее соревнование,
                       pace_zones_json / zones_source / zones_updated_at (персональные зоны)
garmin_recovery_cache — Body Battery, HRV, Training Readiness (TTL 20ч)
workout_notifications — история отправленных анонсов
feedback             — обратная связь (bug / feature)
recommendation_ratings — оценки рекомендаций 1-10 с комментарием
user_activity        — лог команд (/workout, /long, /morning)
workout_analysis     — структурированный анализ анонса (post_id, analyzed_json,
                       extra_groups_json, analysis_mode, edit_date) — кэш Шага 1
bot_settings         — глобальные key-value настройки (preprocess_mode)
```

## Команды бота (основные)
```
/workout   — рекомендация для вт/пт тренировки
/long      — рекомендация для воскресного Long Run
/morning   — утренняя проверка восстановления
/profile   — профиль (VO2max, лактатный порог, пол)
/mode      — режим AI (deep/smart/fast)
/feedback  — обратная связь
/help      — справка

Только админ (273726778):
/stats, /users, /services, /ratings, /feedbacks, /prompt, /debug, /debug_long
/analyze — анализ анонса (Шаг 1, выбор interval/long)
/preprocess_mode — режим анализа тренировок (deep/smart)
/test_workout — тест Шага 2 (recommend_group) на данных админа
/test_long — тест Шага 2 для длительной (recommend_long)
/reanalyze — форс переанализа свежих анонсов (обновить кэш workout_analysis)
```

## Автоматика
| Время МСК | UTC   | Задача |
|-----------|-------|--------|
| 6:45      | 03:45 | `scheduled_cache_refresh` — обновление Strava+Garmin+COROS+Polar для всех |
| 7:00      | 04:00 | `scheduled_morning` — утренняя рассылка (вт, пт, вс) |
| 20:00     | 17:00 | `scheduled_evening` — вечерняя рассылка накануне тренировки (пн, чт, сб) |
| каждые 30 мин | — | `scheduled_new_workout_check` — новые анонсы/доп-группы + фоновый автоанализ (Шаг 1) |

Автоанализ: при новом анонсе / новой доп-группе / редактировании поста (детект по `edit_date`
из Telethon) бот в фоне гоняет `analyze_workout` (прод-режим) и пишет в `workout_analysis` —
кэш готов ДО рассылок, а не при первом /workout.

## Текущая версия
v0.18.2 — см. src/version.py и CHANGELOG.md

## Двухшаговая обработка тренировок (Шаг 1 + Шаг 2 СДЕЛАНЫ, пока только админ)

Идея: вместо парсинга анонса при каждом /workout — один раз анализируем анонс через
DeepSeek (Шаг 1, кэш в БД), затем дёшево комбинируем с данными пользователя (Шаг 2).

### Шаг 1 — анализ анонса (СДЕЛАНО) — `analyze_workout()` в claude_advisor.py
Прод-режим `deep`. Извлекает (interval-схема):
- Валидация (анонс или фото-отчёт/реклама), тип interval/long, дата (год 2026)
- `structure[]` — блоки задаются ОДИН раз: repeat (reps + дистанции + `purpose`) и easy
- `overall_purpose`, `block_contrast`, `target_athlete`, `intensity_level`, `what_to_watch`, `total_volume_km`
- `is_borderline` / `borderline_note` — даёт ли тренировка выбор РАЗНЫХ качеств между группами
- `groups[].blocks[]` — только темпы по блокам (work_pace/recovery_pace/active_recovery),
  `from_comment`, `track_note` (4-я дорожка), `reps_override`, `health_group`
- доп-группы-ссылки из комментариев → `extra_groups[]`
- для long: `has_progression`, `even_pace_available`, группы старым форматом (work-текст)
Форматы времени: точка = мин.сек (1.32 = 92с); готовый темп в скобках используется как есть.

### Персональные зоны — `zones.py` (СДЕЛАНО)
`calculate_pace_zones()` по Дэниелсу/VDOT: easy/marathon/threshold/interval/repetition.
Приоритет источника: лактатный порог → VO2max → Риегель (прогнозы Strava).
Хранятся в `athlete_cache`. Пересчёт: ночной `scheduled_cache_refresh`, ручной ввод VO2max/ЛП,
подключение Garmin/COROS/Polar. `get_pace_zones()` — готовые из БД (или считает на лету).

### Шаг 2 — рекомендация (СДЕЛАНО, детерминированно, без API) — claude_advisor.py
- `recommend_group(analysis, user_data)` (interval): берёт готовые зоны (`get_pace_zones`),
  размечает группы по зонам ЭТОГО пользователя (+ положение внутри зоны: верх/середина/низ),
  считает % подходимости = `полезность_зоны × quality_volume`.
- `quality_volume = intensity × time_to_termination` — интеграл качественной работы до отказа.
  Максимум кривой у порога: слишком медленно → интенсивности нет; слишком быстро → времени нет.
  Метки: оптимум / «тяжело, но реально» / «риск схода» / «низкий стимул» / «слишком легко» /
  «разгрузочный вариант» (на плохом восстановлении грань ближе).
- `полезность_зоны` = характер тренировки (speed/vo2/tempo) × специализация — характер доминирует.
- `is_borderline=false` → основная группа по чистой форме, специализация не влияет.
- `recommend_long(analysis, user_data)` — облегчённая: базовая группа по комфорту на 100 мин +
  соседи (темповее/спокойнее/здоровье), без трёхкомпонентного %.

#### Константы рекомендации (claude_advisor.py, для калибровки по /ratings)
- `_USEFULNESS_FLOOR = 0.80` — полезность зоны мягкий тилт 0.8..1.0
- `_INTENSITY_EXP = 4.0` — крутизна роста интенсивности с темпом
- `_TTT_TAU = 28.0` — сек/км превышения порога, где время до отказа ≈ половина
- `_TTT_STEEP = 3.8` — крутизна обрыва времени до отказа выше порога
- `_TTT_RECOVERY_K = 0.35` — усталость приближает грань отказа
- `_W_FORM_NB, _W_REC_NB = 0.58, 0.42` — веса для non-borderline (по чистой форме)
- `_SPEC_ZONE_VALUE`, `_CHARACTER_ZONE` — таблицы ценности зоны (спец × характер)

### Автоанализ (СДЕЛАНО) — в `scheduled_new_workout_check` (каждые 30 мин)
Новый анонс / новая доп-группа / редактирование поста (`edit_date` Telethon) → фоновый
`analyze_workout` (прод-режим) → `save_workout_analysis`. Кэш готов до рассылок.
Ручной форс — `/reanalyze`.

### Что осталось (TODO)
1. **И3 — интеграция Шага 2 в публичные /workout и /long**: брать готовый анализ из
   `workout_analysis` + зоны + восстановление → `recommend_group`/`recommend_long`.
   Сейчас публичные команды всё ещё идут старым путём; Шаг 2 доступен только через
   /test_workout, /test_long (админ).
2. Генерация .fit-заготовок для всех групп заранее (при автоанализе).
3. Anthropic Claude как альтернативная модель (статусы 'прод'/'тест', переключение админом).
4. Калибровка констант рекомендации по фактам из `recommendation_ratings` + вечерней загрузке.

### Известные нюансы анализа
- coach_notes НЕ содержат подготовку к забегам с датами / индивидуальные советы (→ ignored)
- Год даты тренировки — текущий 2026
- Названия моделей убраны из пользовательских текстов (оставлены в админских командах)
