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
| deep  | deepseek-v4-pro | ~2-3 мин |
| smart | deepseek-chat (с CoT) | ~30-60 сек |
| fast  | deepseek-chat | ~10 сек |

Fallback при ошибке DeepSeek: Groq `llama-3.3-70b-versatile`.

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
user_profile         — VO2max, лактатный порог, пол, источники данных
athlete_cache        — CTL/ATL/TSB, прогнозы Риегеля, последнее соревнование
garmin_recovery_cache — Body Battery, HRV, Training Readiness (TTL 20ч)
workout_notifications — история отправленных анонсов
feedback             — обратная связь (bug / feature)
recommendation_ratings — оценки рекомендаций 1-10 с комментарием
user_activity        — лог команд (/workout, /long, /morning)
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
```

## Автоматика
| Время МСК | UTC   | Задача |
|-----------|-------|--------|
| 6:45      | 03:45 | `scheduled_cache_refresh` — обновление Strava+Garmin+COROS+Polar для всех |
| 7:00      | 04:00 | `scheduled_morning` — утренняя рассылка (вт, пт, вс) |
| 20:00     | 17:00 | `scheduled_evening` — вечерняя рассылка накануне тренировки (пн, чт, сб) |
| каждые 30 мин | — | `scheduled_new_workout_check` — проверка новых анонсов в канале |

## Текущая версия
v0.13.2 — см. src/version.py и CHANGELOG.md

## Следующий большой шаг (ВАЖНО)
**Двухшаговая обработка тренировок:**

1. Когда выходит анонс → бот его ловит и СРАЗУ анализирует через модель
2. Приводит к структурированному формату, понимает суть задания
3. Генерирует .fit файлы для всех групп заранее
4. Хранит всё в БД (таблица `workout_analysis`)
5. Когда приходит пользователь → берём готовый анализ + добавляем его данные
6. Результат: быстрее, надёжнее, не зависит от формата анонса

Это важно потому что сейчас каждый вызов `/workout` парсит анонс заново, что
нестабильно при нестандартных форматах и медленно для первого пользователя.
