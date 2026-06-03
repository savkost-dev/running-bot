# DoDick Bot — Контекст проекта

> Файл-шпаргалка для старта нового чата. Скидывать в начале сессии, чтобы не пересказывать проект заново.
> Последнее обновление: 02.06.2026 (версия 0.21.0, сессия исследования COROS API + метрики).

## Что это
Telegram бот @DD_adviser_bot (имя: DoDick) для бегового клуба Dusty Dumbbells.
Помогает участникам выбрать группу для тренировки на основе их физической формы.

**Фокус аудитории:** ~30 активных пользователей, которые бегают вт/пт интервальные.
Для них бот — основной сервис. Длительная (вс) — приятное дополнение, облегчённая модель.
Те, кто бегают ТОЛЬКО длительную — не в фокусе. На все ~500 участников клуба не ориентируемся.

**Важный нюанс про рекомендации:** тренер ставит структуру тренировки и распределяет
~10-15 «своих» по группам (их темпы в анонсе — калиброванные опорные точки). ОСТАЛЬНЫЕ
выбирают группу сами. Бот делает рекомендации именно для самостоятельных бегунов,
которых тренер не учитывал.

## Инфраструктура
- Сервер: DigitalOcean 167.172.185.88 (2GB RAM, Ubuntu 24.04)
- SSH: `ssh -i $env:USERPROFILE/.ssh/digitalocean root@167.172.185.88`
- Проект локально: D:\running-bot\
- Проект на сервере: `/opt/running-bot/` (НЕ /root/running-bot/ !)
- БД на сервере: `/opt/running-bot/running_bot.db`
- GitHub: github.com/savkost-dev/running-bot (ветка master)
- Деплой: `.\deploy.ps1` — основной инструмент (см. раздел «Деплой» ниже: копирует все *.py, рестарт, проверка, git commit+push)
- Сервис: `systemctl {status|restart} running-bot`
- Логи: `journalctl -u running-bot -n 100 -f`

### Что установлено на сервере
- Python 3.x — есть
- sqlite3 (CLI) — НЕ установлен. Для запросов к БД использовать Python:
  ```bash
  python3 -c "import sqlite3; conn = sqlite3.connect('/opt/running-bot/running_bot.db'); ..."
  ```
- bash — есть

### Деплой — ВСЕГДА через .\deploy.ps1 (основной инструмент)
Запуск из `D:\running-bot`:
```powershell
.\deploy.ps1
```
Что делает за один прогон:
1. Обновляет BUILD_DATE в version.py на сегодня
2. Копирует ВСЕ src/*.py + .env + CHANGELOG.md + CLAUDE.md на сервер (scp)
3. Рестартит сервис (systemctl restart running-bot)
4. Проверяет: health-эндпоинт (версия), OAuth URI Whoop/Strava, ai_mode, хвост логов
5. git add -A + commit (сообщение = первая строка CHANGES из version.py) + push origin master

Перед запуском: обновить version.py (VERSION + первая строка CHANGES = сообщение коммита).
Секреты защищены .gitignore (.env, *.db, *.session, .garmin_token) — в репозиторий не попадают.
git-истории на сервере НЕТ, только на GitHub: github.com/savkost-dev/running-bot (ветка master).

### Деплой отдельного файла (минуя deploy.ps1 — только для быстрого теста)
```powershell
scp -i $env:USERPROFILE\.ssh\digitalocean D:\running-bot\src\FILE.py root@167.172.185.88:/opt/running-bot/src/FILE.py
ssh -i $env:USERPROFILE\.ssh\digitalocean root@167.172.185.88 "systemctl restart running-bot"
```
ВНИМАНИЕ: одиночный scp не коммитит в git и не обновляет версию. Для финального деплоя — всегда deploy.ps1.

## Стек
- Python 3.12, python-telegram-bot (+ APScheduler job_queue), Telethon (единый клиент на процесс)
- DeepSeek API (4 режима: deep/smart/fast + calc), fallback Groq llama-3.3-70b
- SQLite база данных
- Сервисы: Garmin, Polar, COROS (основные), Strava (агрегатор нагрузки + прогнозы), Whoop (заплатка, низкий приоритет)

## Структура проекта
```
src/
  bot.py            — главный файл, все команды и хендлеры
  claude_advisor.py — промпты и вызовы DeepSeek (анализ + рекомендации)
  zones.py          — расчёт персональных темповых зон (Дэниелс)
  telegram_reader.py — чтение канала @Dusty_Dumbbells через Telethon
  strava.py / garmin.py / coros.py / polar.py / whoop.py — сервисы
  weather.py        — OpenWeatherMap
  fit_generator.py  — генерация .fit файлов для Garmin
  oauth_server.py   — веб-сервер для OAuth callback (порт 8080)
  database.py       — SQLite, все таблицы
  version.py        — версионность
```

## Расписание тренировок клуба
- Вторник/Пятница: ИНТЕРВАЛЬНЫЕ (отрезки по дистанции/времени, несколько блоков)
- Воскресенье: LONG RUN 100 минут по темповым группам
- Анонс выходит НАКАНУНЕ (за сутки+) утром в @Dusty_Dumbbells
- Доп. группы (3.5, 5 и пр.) появляются в комментариях к посту в течение дня

## Автоматика (расписание задач)
| Время МСК | UTC   | Задача |
|-----------|-------|--------|
| 6:45      | 03:45 | `scheduled_cache_refresh` — обновление Strava+Garmin+COROS+Polar + пересчёт зон |
| 7:00      | 04:00 | `scheduled_morning` — утренняя рассылка (дни [вт,пт,вс] = weekday 1,4,6) |
| 20:00     | 17:00 | `scheduled_evening` — вечерняя рассылка накануне (дни [пн,чт,сб] = weekday 0,3,5) |
| каждые 30 мин | — | `scheduled_new_workout_check` — поимка новых анонсов + АВТОАНАЛИЗ (Шаг 1) |

Окно 6:45→7:00 (15 мин) для refresh: сейчас запас 87%. ВНИМАНИЕ на будущее: refresh
последовательный (asyncio.sleep(1) на юзера), при 300-400 юзерах окно станет тесным →
распараллелить или сдвинуть refresh раньше.

## === ДВУХШАГОВАЯ ОБРАБОТКА (ядро системы, ПОЛНОСТЬЮ СДЕЛАНА включая И3) ===

### Шаг 1 — Анализ анонса (`analyze_workout` в claude_advisor.py) — СДЕЛАНО
Прод-режим deep. Извлекает структуру, суть, группы, осмысление. Отлажен на реальных анонсах.
Автозапуск: `scheduled_new_workout_check` → `_autoanalyze_post` (фоново). Уведомление только
админу. Idempotency: повтор только при новом post_id / редактировании / новых доп. группах.

**Правила парсинга (выверены на реальных постах):**
- Формат времени: точка = разделитель мин:сек. "1.32" = 1 мин 32 сек. Целое без точки = секунды
- Работа vs восстановление когда оба заданы временем: длиннее/быстрее = work, короче/медленнее = recovery
- active_recovery (логика OR): true если ЛИБО темп быстрее 5:30/км, ЛИБО медленнее работы не более ~50%.
  Нет целевого темпа → active_recovery=false, не фантазировать
- Группа здоровья/ходоков (⏺️ или "бег и ходьба"): ловить по СМЫСЛУ (может быть с номером или без),
  health_group=true, темп не выдумывать
- Доп. группа со ссылкой ("3.5 работает с группой 3") → в extra_groups, без выдуманных отрезков
- Доп. группа с ПОЛНЫМ набором отрезков (группа 5) → в основной groups с from_comment=true
- Орг-инфа (перенос места на будущее, регистрация, чаты, MAX) → в ignored
- Индивидуальные советы с датами забегов → в ignored (не в coach_notes)
- **Пост с упоминанием будущего события, но БЕЗ темпов/групп = не анонс** (is_valid=false)

### Шаг 2 — Рекомендация — СДЕЛАНО (recommend_group / recommend_long)

### И3 — Интеграция в публичный флоу — СДЕЛАНО
`/workout`, `/long` и рассылки работают через кэш `workout_analysis` + `recommend_group`/`recommend_long`.
Старый парсинг на лету убран из пользовательского флоу. `find_next_*` используется ТОЛЬКО для
детекта свежести анонса (post_id/edit_date) — НЕ для парсинга рекомендации.

## === ЗАЩИТА ОТ ЛОЖНЫХ АНОНСОВ (0.21.0) ===

Три слоя защиты против ретроспективных постов и постов без реальных групп/темпов:

**A) Детектор — маркеры ретроспективы в `find_next_long_run` / `find_next_workout`:**
Посты, содержащие слова ("вчера провели", "прошедш", "спасибо за фото", "фотоотчёт" и т.п.),
исключаются на уровне детектора — до вызова LLM.

**B) Failsafe в коде после анализа:**
Если `is_valid=true` но `groups=[]` → принудительно `is_valid=false`.
Для long: хотя бы 1 группа с темпом обязательна для валидного анонса. Не зависит от модели.

**C) Промт Шага 1:**
Явное правило: «пост с упоминанием БУДУЩЕГО события, но БЕЗ темпов/групп = не анонс, is_valid=false».

**D) Фильтр по дню недели:**
Long-анонсы (`find_next_long_run`) принимаются только если дата тренировки = ближайшее воскресенье
(weekday=6). Посты с `workout_date` в другой день недели игнорируются.

## Профиль пользователя
- VO2max (auto из сервиса / manual), лактатный порог (темп + ЧСС, auto/manual), пол
- Специализация: 5k / 10k / half_marathon (default) / marathon / speed / fitness
- **vo2max_locked / lactate_locked** — раздельная защита от перезаписи из сервисов:
  manual автоматически ставит замок; кнопка снятия замка в профиле. ⚠️ Существующие
  manual-записи до 0.20.3 NOT locked по умолчанию (обновляемые).
- Зоны — отображаются в /profile («📊 Мои зоны») с пометкой источника
- Галочка ✅ на текущем выборе в меню (специализация, пол, AI-режим)
- `lactate_source`: `'auto'` (из сервиса) / `'manual'` (ручной ввод). Источник гарнин больше не используется

## Команды бота
```
Пользовательские:
/workout — рекомендация вт/пт | /long — рекомендация вс | /morning — проверка восстановления
/profile — профиль | /mode — режим AI | /feedback | /help

Админ (273726778):
/stats /users /services /ratings /feedbacks /prompt /debug /debug_long
/analyze — анализ анонса (Шаг 1) | /preprocess_mode — режим анализа (deep/smart)
/test_workout /test_long — ТЕСТ Шага 2 на свежем анонсе (постоянные admin-инструменты)
/reanalyze — форс-переанализ последних анонсов обоих типов (игнорит idempotency)
/show_analyze — показать последний Шаг 1 из базы
```

## Режимы рекомендации (/mode) — 4 кнопки
| Режим | Код | Описание |
|---|---|---|
| Глубокий ИИ | `deep` | deepseek-v4-pro, медленно, качественно |
| Быстрый ИИ | `smart` | deepseek-v4-flash (default), быстро |
| Лёгкий ИИ | `fast` | deepseek-chat, самый быстрый |
| Расчётный | `calc` | без ИИ, только формула (0 токенов) |
Fallback для всех ИИ-режимов: Groq `llama-3.3-70b-versatile`.

## Формула рекомендации (calibrated, v0.20.5)
**Итоговый % = полезность_зоны × quality_volume**
- `quality_volume = intensity(pace) × time_to_termination(pace, recovery)`, нормировано к пику
- `time_to_termination`: плато ниже порога, РЕЗКИЙ нелинейный обрыв выше порога
- Формула: `1/(1+(Δ/τ)^k)`, где Δ — превышение порога в сек/км

**Актуальные константы:**
```
_USEFULNESS_FLOOR = 0.72    # тилт полезности зоны (0.72..1.0)
_INTENSITY_EXP = 4.0        # крутизна роста интенсивности
_TTT_TAU = 28.0             # сек/км превышения порога → половина времени до отказа
_TTT_STEEP = 5.5            # крутизна нелинейного обрыва выше порога
_TTT_RECOVERY_K = 0.35      # поправка на восстановление
_W_FORM_NB/_W_REC_NB = 0.58/0.42  # веса форма/восстановление (non-borderline)
```

**Двухцветная шкала:** второй ряд 🟦 для групп где alt_pct − pct ≥ 12 (значимая альтернатива
по другой специализации). `alt_pct` считается как чистая ценность зоны для альт-спец
(НЕ умножается на qvnorm — не зависит от усталости). 🟩 — по силам сегодня, 🟦 — ценность
зоны для другой цели.

## Таблицы БД (ключевые)
```
users, user_tokens, user_preferences (ai_mode DEFAULT 'smart', is_active, deactivated_at)
user_profile (VO2max, ЛП, пол, источники, specialization, vo2max_locked, lactate_locked)  ⚠ ЕД.Ч.!
athlete_cache (CTL/ATL/TSB, Риегель, + pace_zones_json/zones_source/zones_updated_at)
garmin_recovery_cache (Body Battery, HRV, Training Readiness, TTL 20ч)
workout_analysis (post_id, workout_date, workout_type, is_valid, analyzed_json, extra_groups_json,
                  analysis_mode, created_at, edit_date) — кэш анализа. НЕ чистится (для оценок)
workout_notifications, feedback, recommendation_ratings, user_activity, bot_settings
```

## Мягкая деактивация
Forbidden при отправке → is_active=0 + deactivated_at. Входящее сообщение → is_active=1.
Рассылки используют get_active_users() / get_all_users_with_status().

## Разделение рассылки (has_data)
- has_data=True (есть VO2max ИЛИ токен трекера) → полная рекомендация
- has_data=False (пустой профиль) → упрощённое уведомление + призыв заполнить профиль

## === АРХИТЕКТУРА ДВУХ ВАРИАНТОВ РЕКОМЕНДАЦИИ ===

Вариант B существует ТОЛЬКО для эксперимента — получает только админ.
Вариант A — это прод, его не трогаем.

### Реализованная архитектура

**Вариант A** (формулы + ИИ-проза) — прод, работает:
```
analyze_workout → recommend_group → recommendation_to_advice → format_evening_message
```

**Вариант B** (чистый ИИ) — СДЕЛАНО, работает через общий рендерер:
```
analyze_workout → build_ai_b_prompt → ask_groq → advice_dict → format_evening_message  (В1)
                                                              → generate_ai_b_extra     (В2)
```

**Три сообщения для админа при /workout:**
- **А1** — стандартная рекомендация (как у всех пользователей), вариант A
- **В1** — та же структура/шаблон, но группу выбирает чистый ИИ (без формул)
- **В2** — свободный текст от ИИ: физиология, нюансы, совет на первые отрезки

### Функции в claude_advisor.py
- `build_ai_b_prompt(analysis, user_data, zones_map, recovery)` → строка промпта
- `ask_groq(prompt, mode)` → `{"advice": dict, "stats": dict}` — используется для B1
- `generate_ai_b_extra(analysis, advice, mode)` → строка HTML-текста — используется для B2
- `format_evening_message(advice, workout, stats, weather_line, has_tracker)` — единый рендерер

### Функция в bot.py
`_send_ai_variant_b(telegram_id, analysis, user_data, context, workout_dict, weather_line)`
Запускается через `asyncio.create_task` после основного сообщения А1.
Принимает `workout_dict` и `weather_line` от вызывающего кода (не переформировывает сама).

### Обязательные поля advice-dict (вечерняя форма)
```python
{
    "recommended_group": "3",
    "recommended_pace": "4:00–4:15 мин/км",
    "reason": "...",
    "if_feeling_good": "...",
    "if_tired": "...",
    "gap_note": "...",
    "suitability_percentages": [{"group": "3", "percentage": 85, "comment": "идеально"}],
    "preparation_tips": ["..."],
    "warning": None,
    "spec_label": "полумарафон",
    "overall_purpose": "...",   # из analysis["overall_purpose"], подставляется в коде
    "workout_summary": "...",   # из analysis["summary"], подставляется в коде
}
```

### PENDING — баг В2 не приходит

**Диагноз (02.06.2026):** `generate_ai_b_extra` использует DeepSeek (`_get_client()`),
а не Groq. Модель `deepseek-v4-flash` — thinking-модель. При `temperature=0.5` и
`max_tokens=800` она может потратить все токены на `<think>...</think>`, после чего
`content` будет пустым. После `_re.sub` останется пустая строка — функция возвращает `""`
без какого-либо логирования. Бот тихо пропускает второе сообщение.

**Фикс (НЕ задеплоен):** в `generate_ai_b_extra` заменить блок `try/except`:
```python
    try:
        resp = _get_client().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tok,
            temperature=0.5,
            timeout=timeout,
        )
        msg = resp.choices[0].message
        raw = (msg.content or "").strip()
        raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
        # Fallback: если content пустой — пробуем reasoning_content
        if not raw:
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning:
                raw = reasoning.strip()
                logger.info("generate_ai_b_extra: использован reasoning_content как fallback")
        if not raw:
            logger.warning("generate_ai_b_extra: пустой ответ модели")
            return ""
        return raw
    except Exception as e:
        logger.warning(f"generate_ai_b_extra error: {e}")
        return ""
```
После деплоя — проверить логи: либо придёт В2, либо будет `generate_ai_b_extra: пустой ответ модели`.

### Технические заметки
- MCP filesystem не делает str_replace на Windows-путях с кириллицей — несовпадения.
  Редактировать через `filesystem:write_file` (полный файл) или PowerShell на Windows.
- MCP read_file читает весь файл в контекст — сжирает токены. Для отладки скидывать
  только нужные функции копипастом, не читать файлы через MCP.
- Копировать файл через scp (deploy.ps1), не через PowerShell-пайп (mojibake).




## === АРХИТЕКТУРА ДАННЫХ — ЦЕЛЕВАЯ (спроектировано 02.06.2026) ===

### Три слоя

**Слой 1 — Raw Fetch** (сервисные файлы: garmin.py, coros.py, strava.py, whoop.py, polar.py)
- Тянут данные как есть, сохраняют сырой JSON в БД (новое поле raw_json в кэше)
- Никакого парсинга и преобразований — только fetch + сохранение
- Сырые данные хранятся: весят мало, помогают при отладке и будущих изменениях

**Слой 2 — Normalize** (новый файл: data_normalizer.py)
- Запускается сразу после fetch (не по запросу)
- Читает сырой JSON, парсит в единый формат UnifiedUserData
- Сохраняет нормализованные данные в БД (новая таблица: unified_cache)
- Каждый сервис имеет свой normalizer: normalize_garmin(), normalize_coros() и т.д.

**Слой 3 — Consume** (claude_advisor.py, bot.py)
- Читает только UnifiedUserData из unified_cache
- Не знает откуда данные пришли (Garmin/COROS/Whoop — неважно)
- Добавление нового сервиса = написать fetch + normalize, промпт не трогать

### Единый формат UnifiedUserData (только универсальные поля)
```python
{
    # Форма — источник зон и рекомендации группы
    "lactate_threshold_pace": "4:04",  # мин:сек/км, строка
    "lactate_threshold_hr": 172,       # уд/мин
    "vo2max": 54.0,                    # мл/кг/мин
    "zones": {                         # рассчитывается в zones.py
        "easy": "5:30",
        "marathon": "4:35",
        "threshold": "4:04",
        "interval": "3:42",
        "repetition": "3:26",
    },

    # Восстановление — единая шкала 0–100 для всех
    "recovery_score": 99,   # 0–100: Whoop нативный, COROS recoveryPct,
                            # Polar ANS recharge, Garmin из BB (100-BB/100*100)
    "hrv": 87.0,            # мс, ночной
    "hrv_baseline": 95.0,   # 7-дневная база (если есть)
    "rhr": 44,              # ЧСС покоя

    # Нагрузка
    "load_48h": {
        "sessions_48h": 1,
        "total_km_48h": 8.2,
        "last_activity_hours_ago": 14,
        "intensity": "moderate",  # low/moderate/high — нормализованный уровень
    },
    "training_load": {      # CTL/ATL/TSB если есть у сервиса, None если нет
        "ctl": 45.0,
        "atl": 52.0,
        "tsb": -7.0,
        "summary": "небольшая усталость",
    },

    # Мета
    "sources": ["coros"],          # какие сервисы дали данные
                                   # Strava — агрегатор активностей, не источник биометрии.
                                   # Участвует только в load_48h и training_load (CTL/ATL/TSB)
    "source_priority": {           # приоритет при конфликте (настраивается)
        "recovery_score": ["coros", "polar", "garmin", "whoop"],
        "hrv":            ["garmin", "coros", "polar", "whoop"],
        "vo2max":         ["garmin", "coros", "polar", "strava"],
        "load_48h":       ["garmin", "coros", "strava"],
        "training_load":  ["strava", "garmin", "coros"],
    },
    "updated_at": "2026-06-02T16:00:00",
}
```

### Неуниверсальные поля — выпилить из общего знаменателя
Следующие поля специфичны для конкретного сервиса. Сейчас НЕ включаются в UnifiedUserData.
Добавляются позже как «фича сервиса» когда будет понятна ценность:
- `body_battery` — только Garmin. ВЫПИЛИТЬ ИЗ ПРОМПТА (слишком тонкая фишка, мешает).
- `training_readiness` — только Garmin (убрана из формулы, смещение). Остаётся в промпте как текст.
- `sleep_hours`, `sleep_score` — Whoop, Polar. Фича этих сервисов.
- `suffer_score` — внутренний расчёт Strava, используется внутри load_48h для определения intensity, наружу не выходит.
- `stamina_level`, `evolab_scores` — только COROS EvoLab. Фича COROS.
- `recovery_time_h` — только COROS (fullRecoveryHours). Фича COROS.
- `ans_rate` — только Polar. Фича Polar.

### Приоритизация при конфликте
`source_priority` в UnifiedUserData — упорядоченный список сервисов для каждого поля.
При наличии нескольких подключённых сервисов берётся первый у кого есть данные.
Готов к настройке в любой момент без изменения кода промпта.

Пример: у пользователя Garmin + Whoop.
- `recovery_score`: берётся Whoop (приоритет выше для этого поля)
- `hrv`: берётся Whoop
- `vo2max`: берётся Garmin
- `load_48h`: берётся Garmin

### Статус реализации
⏳ Спроектировано, не реализовано (02.06.2026).
Текущий код: каждый сервис парсит напрямую в промпт, нет единого слоя нормализации.
Переход к новой архитектуре — отдельная задача, не трогать текущий флоу до готовности.

## === COROS API — статус и архитектура (02.06.2026) ===

### Рабочий endpoint
`GET https://teamapi.coros.com/dashboard/query` — возвращает EvoLab данные.
Авторизация: заголовок `accesstoken: TOKEN` (токен из логина через MD5 пароля).
Домен зависит от региона: `teamapi` (US/глобал), `teameuapi` (EU) — токен привязан к региону.

### Что возвращает /dashboard/query → summaryInfo
```
ltsp: 244          # лактатный порог в сек/км → '4:04' мин/км (ключ для зон!)
lthr: 172          # лактатный порог ЧСС
recoveryPct: 99    # нативный Recovery % (0–100) — аналог Whoop Recovery Score
recoveryState: 4   # уровень: 1=истощён, 2=уставший, 3=хороший, 4=отличный
rhr: 44            # ЧСС покоя
fullRecoveryHours: 2  # часов до полного восстановления
sleepHrvData:
  avgSleepHrv: 87  # HRV последней ночи (мс, RMSSD)
  sleepHrvBase: 95 # базовый HRV (скользящая 7 дней)
  sleepHrvList: [] # история по дням за 7 дней
staminaLevel: 94.5 # уровень выносливости
aerobicEnduranceScore, anaerobicCapacityScore, etc. — EvoLab скоры
```
VO2max в этом endpoint НЕ приходит. Считается из ltsp через VDOT (zones.py).

### Дополнительный рабочий endpoint
`GET https://teamapi.coros.com/activity/query?size=N&pageNumber=1` — список активностей.
Поля: `trainingLoad`, `avgHr`, `avgSpeed`, `distance`, `totalTime`, `date`, `startTime` — для нагрузки 48ч.

### Что НЕ работает (5C4D208)
`/sport/dailySummary/query`, `/analyse/training/load/query`, `/health/hrv/query`,
`/v2/coros/sport/detail/dayDetail` и все `/dashboard/fitnessOverview`, `/dashboard/fitness` и т.п.
Код 5C4D208 = endpoint не поддерживается для данного аккаунта/региона.

### Официальный API
Заявка подана 02.06.2026 через Feishu-форму (ссылка из support.coros.com).
Запрошено: Activity Sync (one way), Access Daily Health Data, Structured Workouts Sync.
До одобрения — работаем через неофициальный /dashboard/query.

### Функция get_dashboard_data (coros.py)
Тянет /dashboard/query и возвращает:
- `lactate_threshold_pace` — из ltsp
- `lactate_threshold_hr` — из lthr  
- `recovery_score` — из recoveryPct (нативный!)
- `hrv` — из sleepHrvData.avgSleepHrv
- `rhr` — ЧСС покоя
- EvoLab скоры формы

## === ОБЩИЙ ЗНАМЕНАТЕЛЬ МЕТРИК (все сервисы) ===

### В промпт идут (у всех сервисов)
| Поле | Garmin | Strava | Whoop | COROS | Polar |
|---|---|---|---|---|---|
| ЛП темп → зоны | auto/manual | ~ VDOT из прогнозов | — | ltsp нативный | — |
| VO2max | нативный | ~ VDOT из прогнозов | — | ~ из ltsp | нативный |
| recovery_score | — | — | нативный* | recoveryPct | ANS recharge |
| HRV | ночной | — | ночной* | avgSleepHrv | RMSSD |
| нагрузка 48ч | есть | есть | — | из activity/query | — |
| CTL/ATL/TSB | нативный | своя метрика | — | ~ из активностей | — |

*Whoop — заплатка, низкий приоритет разработки

### Только у конкретных сервисов (в промпте если есть)
- **Body Battery** — только Garmin
- **Sleep hours** — Whoop, Polar
- **CTL/ATL/TSB** — Strava (нативный), Garmin (своя метрика)
- **Suffer Score** — только Strava

### Training Readiness
Убрана из формулы (только Garmin, смещение при частичной доступности).
Остаётся в промпте как текстовый контекст.

## === TODO / ПЛАНЫ / МЫСЛИ НА БУДУЩЕЕ ===

### Восстановление — убрать из вечерней рекомендации совсем (идея 02.06.2026)
Восстановление за ночь сильно меняется. Вечерняя рекомендация на вечерних данных
восстановления = неточно (как было с Body Battery). Идея: убрать ВСЁ восстановление
из вечерней рекомендации, перенести только в утреннюю рассылку (где данные свежие
и за ночь не успеют поменяться). Вечером — только форма/зоны/нагрузка.

### Разобраться как работает утренняя рассылка (большой запрос)
Отдельная сессия: детально разобрать механику build_morning_prompt, тайминги,
cache_refresh (6:45), что подаётся, как считается recovery_level. Связано с идеей
выше — если переносим восстановление в утреннюю, надо понимать её устройство.

### Workout-прогноз — отдать полностью ИИ (почти готово)
Сейчас прогноз тренировки разлетается с формульным подходом в основном из-за
восстановления. Дать ИИ делать полный прогноз. Большая часть уже готова.

### Калибровка формулы по ИИ (двойной расчёт)
Для каждого человека делать два расчёта (как сейчас для админа): формульный и ИИ.
Пользователю слать тот что у него в настройках, в базе копить статистику обоих.
Так подправится форма кривой формулы на реальных данных по каждому человеку.

### _recovery_value в формуле — берёт суточное, переделать на слое 3
recommend_group использует _recovery_value(recovery) → одно число 0-100, по приоритету:
recovery_score → training_readiness → readiness → score (score = технический fallback
для вложенных dict вида {"score": N}).
Проблема: разные по СМЫСЛУ метрики попадают в одну переменную формулы:
- Garmin-юзер → training_readiness (длительное)
- COROS/Polar-юзер → recovery_score (суточное, COROS recoveryPct)
Шкала у всех 0-100, формула не падает, но смысл разный.
Длительное для COROS сейчас взять НЕОТКУДА: ati/cti дают 500, Strava TSB несовместим
по шкале (±, не 0-100). Менять приоритеты сейчас почти бесполезно (у юзера обычно
одна метрика из двух). Решается слоем 3: формула будет брать чёткое поле
(recovery_total для длительного), а не "что первое попалось". НЕ трогать до слоя 3.

### Блок восстановления в промптах унифицируется со слоем 3
Сейчас вечерние промпты (build_evening_prompt и build_ai_b_prompt) формируют блок
восстановления ситуационно и по-разному (делят на garmin/else и т.п.). Это НЕ чинить
отдельно — унификация придёт автоматически когда промпт начнёт брать данные из
UnifiedUserData (слой 3). Слой 3 для того и готовится.
Точечно убраны Body Battery и HRV из обоих вечерних промптов (03.06.2026) — мешали,
индивидуальны без диапазона. Остальное оставлено как есть до слоя 3.

### Дата рождения / пол — ЗАПОЛНЕНО (03.06.2026)
backfill_profile.py заполнил пол+ДР для 13 юзеров. Хранятся в user_profile
(gender, birthdate — статичны, разово). Запуск: python3 backfill_profile.py [--dry].
get_profile во всех сервисах (единый формат male/female + YYYY-MM-DD).
Не заполнен только user 5 (Strava-only, ДР не отдаёт — нужен ручной ввод).
COROS sex=0=муж/1=жен ПОДТВЕРЖДЕНО (Karpov муж / Ксения,Истомина жен).

### Garmin rate-limit 429 при массовом релогине (заметка)
Garmin авто-релогин (_reauth в _client) работает, но Garmin отдаёт 429 "IP rate limited"
на mobile-логин. При единичных релогинах проходит (падает на запасной метод). Но если
МНОГО токенов протухнет разом (напр. все в cache_refresh) — Garmin может временно
забанить IP сервера. TODO: добавить задержку между релогинами или не делать пачкой.

### Слой 1.1 fetch_raw — ТОЛЬКО сырьё as is (переделано 03.06.2026)
ВАЖНО: fetch_raw должен звать СЫРЫЕ API напрямую, НЕ наши get_* обёртки.
Была ошибка: первая версия fetch_raw звала get_training_readiness и т.п., которые
уже распарсили и ВЫКИНУЛИ timestamp'ы — в raw попадал огрызок без дат.
Исправлено: fetch_raw зовёт сырые методы и кладёт нетронутый ответ.
- Garmin: client.get_max_metrics/get_training_status/get_training_readiness/get_user_summary/
  get_hrv_data/get_lactate_threshold/get_activities_by_date/get_user_profile (сырые методы garminconnect)
- COROS: _get на /dashboard/query, /account/query, /activity/query (сырой JSON)
- Strava: /athlete + /athlete/activities (CTL/ATL/прогнозы УБРАНЫ — это наши расчёты, не сырьё!)
- Polar: /users/{id}, /users/nightly-recharge, /users/sleep
Теперь raw_service_data хранит исходные timestamp'ы: Garmin training_readiness несёт
timestamp+timestampLocal, Strava activities — start_date+start_date_local, и т.д.
Зачем: для рекомендаций критично ЗНАТЬ когда метрика зафиксирована в источнике
(не наше время загрузки). Юзер мог не синхронить часы 3 дня — "свежий" TR на деле старый.
Извлечение source-времени из сырья — задача слоя 2.

### Polar physical-info (VO2max + пороги) — событийный pull, ловить в fetch_raw
VO2max (фитнес-тест), ЛП по пульсу (anaerobic-threshold), max_hr, rhr лежат в Polar
physical-information, НЕ в профиле. Достаются через transaction-механику (POST создать →
GET список → GET запись → PUT commit). Путь С userId: /v3/users/{pid}/physical-information-transactions.
Функция get_physical_info в polar.py готова.
ВАЖНО — pull-механика: каждая запись отдаётся ОДИН РАЗ. После прочтения (или коммита
транзакции) данные считаются доставленными, новый POST даёт 204 "нет новых". Текущее
значение повторно НЕ достать — только при следующем фитнес-тесте появится новая запись.
ВЫВОД: get_physical_info надо вызывать в регулярном fetch_raw и СРАЗУ сохранять результат
в свою БД (user_profile: vo2max, lactate_threshold_hr). Вычитал раз — записал навсегда.
Нельзя дёргать "по запросу" и ждать текущее значение.
Поля physical-info: vo2-max, maximum-heart-rate, resting-heart-rate, aerobic-threshold (ЧСС),
anaerobic-threshold (ЧСС ≈ ЛП по пульсу), weight, height.
Игорю (user 7) VO2max=50 + ЛП_hr=182 вписаны ВРУЧНУЮ 03.06.2026 (данные уже были вычитаны тестами).

### Пол и возраст в промпт (03.06.2026)
В промпте сейчас НЕТ возраста, пол учитывается частично (gender_line в build_evening_prompt).
Возраст важен — нормы VO2max и макс. пульс зависят от возраста (VO2max 50 у 25-летнего
и 50-летнего = разный уровень). Polar отдаёт оба в профиле: birthdate + gender.
Сделать: вытащить gender/age из профилей сервисов (Polar точно даёт, проверить Garmin/COROS),
прокинуть в промпт строкой возраста рядом с полом. Затрагивает прод-флоу — делать аккуратно.

### Polar Running Index (VO2max) — отдельный запрос
VO2max (МПК) Polar в профиле НЕ отдаёт (только вес/рост/пол/дата рождения).
Polar Running Index считается из забегов, лежит в детализации тренировки (?samples=true),
не в базовом /v3/exercises. Пока проще спросить МПК у пользователя вручную.
Если понадобится автоматом — копать детализацию exercise или брать из Strava (VDOT).



### Большой блок — Вечерняя загрузка фактов (петля обратной связи)
Вечером после тренировки подтягивать активность из сервиса → смотреть как реально прошло.
Замыкает петлю: бот советовал группу N → факт показывает что реально держал/добежал/где сдох.
Даёт: проверку рекомендаций, персональную адаптацию, обновление формы по фактическим темпам.
Тяжёлый по нагрузке (тянуть .fit/gpx каждого) — учесть в таймингах.

### Мультимодельность (DeepSeek + Anthropic) — спланировано, не сделано
Два слота: preprocess и recommend. Тест работает параллельно проду, результат в БД.
Сейчас claude_advisor завязан на DeepSeek через OpenAI SDK — Anthropic нужен другой SDK.

### ⚠️ ОНБОРДИНГ И ПОСТАВЩИКИ ДАННЫХ — узкое место
- **Strava** задумывалась как универсальный вход (OAuth). НО: лимит API = 1 пользователь.
  Заявка на 100 подана, висит. Срок одобрения неизвестен.
- **Garmin** работает через логин+пароль — люди боятся вводить. Основной трекер.
- **COROS** работает через логин+пароль. Рабочий endpoint: `/dashboard/query`. Заявка на официальный API подана 02.06.2026. Основной трекер.
- **Polar** — OAuth, работает (починен 03.06.2026). Основной трекер для 1 пользователя.
- **Strava** — агрегатор: нагрузка 48ч, CTL/ATL/TSB, прогнозы забегов → VDOT. Лимит API 1 юзер, заявка на 100 подана.
- **Whoop** — низкий приоритет, заплатка. Подключается но не в фокусе разработки.
- **Ручной ввод** (Риегель → зоны) есть, но подаётся как fallback — должен быть первым классом.

Направления:
1. Ручной ввод (пол + результат на дистанции / ЛП) — гладкий путь, не fallback
2. Снять страх логин/пароля: объяснить в момент подключения для чего и где хранится
3. Онбординг с первого сообщения ведёт за руку (развилка: трекер vs пара вопросов)
4. Не завязывать выживание продукта на Strava

### Мониторинг рассылки
Баг `user_profiles` (→ `user_profile`) жил незамеченным с 0.12.0 — рассылки тихо падали.
Нужен health-check: лог «рассылка ушла N юзерам» + алерт если упала.

### Масштабирование (когда база вырастет с ~41 до сотен)
- Распараллелить cache_refresh (сейчас последовательный sleep(1)/юзер)
- Развести тяжёлые операции (автоанализ, вечерняя загрузка) по времени

## Уроки (чтобы не повторять)
- Деплой/рестарт около времени рассылки может её сбить. Деплоить в спокойное время
- Опечатка в имени таблицы роняла рассылку, но команды работали — баг незаметен.
  Проверять рассылочный путь отдельно
- PowerShell `Get-Content` без `-Encoding UTF8` ломает кириллицу → mojibake в py-файлах.
  Всегда копировать через scp (deploy.ps1), не через PowerShell-пайп
- Калибровать формулы на реальных GPX-данных, не на догадках
- Ретроспективный пост с упоминанием будущего события может пройти детектор анонсов —
  нужны failsafe-фильтры на уровне кода (не только промт). Решено в 0.21.0 тремя слоями.
- MCP read_file съедает контекст чата целиком — скидывать только нужные функции копипастом
- COROS: teamapi.coros.com стучать через GET /dashboard/query. Другие endpoint-ы дают 5C4D208. Домен зависит от региона токена (teamapi vs teameuapi)
- COROS: sqlite3 CLI не установлен на сервере — только через python3. Venv: /opt/running-bot/venv/bin/python3
- Polar AccessLink v3: путь БЕЗ userId — /v3/users/nightly-recharge и /v3/users/sleep (токен сам определяет юзера). С userId в пути → 404. Поля через подчёркивание: ans_charge, heart_rate_variability_avg, heart_rate_avg, sleep_score, light_sleep/deep_sleep/rem_sleep (секунды). ANS charge шкала -10..+10 → recovery 0-100 через (ans+10)/20*100
