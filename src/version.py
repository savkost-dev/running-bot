VERSION = "0.24.63"
BUILD_DATE = "2026-06-07"
CHANGES = [
    "Снимок «на утро»: убран garmin_recovery_cache из _collect_morning_snapshot. "
    "Все Garmin-поля теперь строго из сырья (слой 1): TR, HRV (hrv_data.hrvSummary.lastNightAvg), "
    "BB, RHR, сон, пробуждение. Кэш создавал пробелы (у части юзеров пуст) — теперь полнота "
    "снимка одинакова у всех при обработанной ночи.",
]
