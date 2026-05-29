# Running Bot — правила для Claude

## Структура проекта

```
running-bot/
├── src/
│   ├── bot.py              # Главный файл: handlers, schedulers, main()
│   ├── claude_advisor.py   # Промты, DeepSeek/Groq вызовы, форматирование
│   ├── strava.py           # Strava API: активности, CTL/ATL, 48h нагрузка
│   ├── garmin.py           # Garmin Connect API: метрики, FIT загрузка
│   ├── fit_generator.py    # Генерация .fit файлов из JSON тренировки
│   ├── database.py         # SQLite: пользователи, токены, кэш
│   ├── whoop.py            # Whoop API: Recovery, HRV, Sleep
│   └── version.py          # Версия и список изменений
├── CHANGELOG.md            # История версий
├── CLAUDE.md               # Этот файл
└── deploy.ps1              # Деплой на сервер
```

## Сервер

- **IP**: 167.172.185.88
- **Путь**: `/opt/running-bot/`
- **Сервис**: `systemctl status/restart running-bot`
- **Логи**: `journalctl -u running-bot -n 100 -f`
- **SSH**: `ssh -i C:/Users/savko/.ssh/digitalocean root@167.172.185.88`

## Деплой

```powershell
# ✅ ЕДИНСТВЕННЫЙ правильный способ деплоя:
.\deploy.ps1
```

> ⚠️ **НИКОГДА не делать `scp` отдельных `.py` файлов вручную.**
> Это главная причина расхождений между локальным кодом и сервером.
> `deploy.ps1` копирует ВСЕ `*.py` разом и выводит верификацию ключевых настроек.

### Git: автоматический коммит при деплое

`deploy.ps1` автоматически делает `git commit` и `git push` после каждого успешного деплоя.

- **Формат коммита**: `deploy: v{VERSION} - {первая строка из CHANGES}`
- **Ветка**: `master` → `origin/master`
- Если нечего коммитить — пишет `INFO: Nothing to commit` и продолжает

> После каждого деплоя на сервер автоматически делать git commit и push.
> Формат коммита: `deploy: v{VERSION} - {краткое описание изменений}`

После деплоя скрипт автоматически проверяет:
- версию на сервере (совпадает ли с локальной)
- `WHOOP_REDIRECT_URI` в `whoop.py`
- `OAUTH_REDIRECT_BASE` в `strava.py`
- `ai_mode DEFAULT` в `database.py`
- последние строки лога

## Версионность

### Схема: MAJOR.MINOR.PATCH

| Тип изменения | Пример | Версия |
|---|---|---|
| Критическая архитектура | новая БД, смена AI провайдера | MAJOR (X.0.0) |
| Новая фича | новый источник данных, новая команда | MINOR (0.X.0) |
| Баг-фикс, мелкий твик | правка промта, исправление парсера | PATCH (0.0.X) |

### После ЛЮБЫХ изменений обязательно:

1. **Обновить `src/version.py`**:
   ```python
   VERSION = "0.5.1"          # новая версия
   BUILD_DATE = "2026-05-25"  # сегодня
   CHANGES = ["Описание изменений"]
   ```

2. **Добавить запись в `CHANGELOG.md`**:
   ```markdown
   ## [0.5.1] 2026-05-25 — Краткое описание
   ### Добавлено / Исправлено
   - Что конкретно изменилось
   ```

3. **Задеплоить**: `.\deploy.ps1`

### Текущая версия: 0.6.2

---

## Ключевые технические детали

### AI модели
- **Deep**: `deepseek-v4-pro` (MODEL_DEEP) — медленно, качественно
- **Fast**: `deepseek-chat` (MODEL_FAST) — быстро, режим по умолчанию
- Fallback: Groq (`llama-3.3-70b-versatile`)

### Schedulers (APScheduler)
| Время (UTC) | Функция | Описание |
|---|---|---|
| 17:00 | `scheduled_evening` | Вечерние рекомендации |
| 04:00 | `scheduled_morning` | Утренние данные |
| 01:00 | `scheduled_strava_cache` | CTL/ATL/TSB обновление |
| 03:00 | `scheduled_data_refresh` | Обновление всех данных |
| каждые 30 мин | `scheduled_new_workout_check` | Проверка новых тренировок |

### Garmin данные
- **Динамичные** (каждый запрос): Training Readiness, Body Battery, HRV
- **Статичные** (раз в 24ч): VO2max, Training Status
- Кэш контролируется через `vo2max_updated_at` в профиле

### FIT generator — форматы темпа
| Формат | Пример | Интерпретация |
|---|---|---|
| Секунды | `137–130 сек` | Явное время в секундах |
| M:SS как время | `2:17–2:10` | Время на дистанцию (>5.5 м/с → reinterpret) |
| M.SS точка | `4.05–3.40` | Темп мин/км с точкой вместо двоеточия |

### Обработка ошибок
- `BadRequest: Message is not modified` — двойной клик по кнопке, молча игнорируется
- `TimedOut`, `NetworkError` — логируются как warning, не как error
- Все остальные — `logger.error` с traceback
