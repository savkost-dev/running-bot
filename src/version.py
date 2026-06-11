VERSION = "0.24.83"
BUILD_DATE = "2026-06-11"
CHANGES = [
    "Расчётный TR в утреннем слепке: _collect_morning_snapshot для не-Garmin юзеров "
    "берёт TR из unified (coros-calc/strava-calc); порядок поимки изменён — "
    "сначала нормализация, потом сборка снимка (оба места wakeup_poll).",
    "0.24.82: TR строже — только при сегодняшнем TSB; без TSB TR отсутствует.",
    "0.24.81: COROS /analyse/query (ati/cti за сегодня) в сырье; родной Form приоритетнее Strava TSB."
]
