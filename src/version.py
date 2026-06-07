VERSION = "0.24.58"
BUILD_DATE = "2026-06-07"
CHANGES = [
    "Снимок «на утро»: при поимке ночи опросник замораживает TR/BB/HRV/RHR/длину сна + "
    "время пробуждения + время снимка вместе с флагом morning_caught (колонки в unified_cache).",
    "Сборщик _collect_morning_snapshot: garmin (BB на пробуждении, сон, RHR, wake-time из "
    "wellnessEndTimeLocal; TR/HRV из garmin_recovery_cache) → whoop → polar → coros, "
    "первый не-None по каждому полю.",
    "БД: set_morning_caught принимает snapshot, get_morning_caught отдаёт снимок. "
    "Колонки morning_tr/bb/hrv/rhr/sleep_h/wake_at/snapshot_at.",
]
