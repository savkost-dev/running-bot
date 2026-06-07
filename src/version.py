VERSION = "0.24.56"
BUILD_DATE = "2026-06-07"
CHANGES = [
    "Whoop затащен в конвейер слоёв 1-2 как источник данных за прошлую ночь: "
    "fetch_raw (сырьё recovery+sleep в raw_service_data), _parse_whoop_raw + normalize_whoop "
    "(HRV, ЧСС покоя, сон + дата ночи). В мёрдже Whoop даёт HRV/RHR/сон (приоритет — носимый ночью), "
    "дата ночи в data_dates.whoop_measured. whoop.fetch_raw добавлен в плановый сбор 06:45/07:15.",
    "Извлечение bodyBatteryAtWakeTime в Слое 2 (data_normalizer) — для детектора пробуждения.",
    "Миграция unified_cache: колонки morning_caught, morning_date (детектор «поймали ночь»).",
]
