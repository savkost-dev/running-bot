VERSION = "0.24.63"
BUILD_DATE = "2026-06-07"
CHANGES = [
    "Снимок «на утро» Garmin HRV: берём из сырья hrv_data.hrvSummary.lastNightAvg "
    "(есть у всех Garmin-юзеров), фолбэк на garmin_recovery_cache. Раньше HRV брался "
    "только из кэша, который у части юзеров пуст → HRV=None в снимке.",
]
