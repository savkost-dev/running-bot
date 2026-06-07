VERSION = "0.24.57"
BUILD_DATE = "2026-06-07"
CHANGES = [
    "Опросник пробуждения (scheduled_wakeup_poll): каждые 15 мин в окне 06:00–09:00 МСК. "
    "Для юзеров с ночным сервисом (garmin/coros/polar/whoop, не strava), у кого сегодня ночь ещё "
    "не поймана — синкает сырьё (слой 1) и перепроверяет готовность. Поймал → ставит флаг "
    "morning_caught и исключает до завтра. Только забор данных, нормализация не трогается.",
    "Детектор готовности ночи _night_ready: garmin wake-time / coros sleepHrv / polar дата / whoop дата.",
    "БД: set_morning_caught / get_morning_caught (флаг в unified_cache, сбрасывается сменой даты).",
]
