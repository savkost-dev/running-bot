VERSION = "0.24.67"
BUILD_DATE = "2026-06-07"
CHANGES = [
    "Рефакторинг: recovery-домен вынесен из bot.py в новый модуль recovery.py "
    "(6 функций: _update_garmin_recovery_from_raw, _fetch_garmin_recovery, _get_recovery_data, "
    "_garmin_observation_end, _get_unified_recovery, _recovery_scenario). Логика без изменений, "
    "только перенос. bot.py: 5237 → 4901 строк.",
]
