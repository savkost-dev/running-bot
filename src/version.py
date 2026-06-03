VERSION = "0.24.0"
BUILD_DATE = "2026-06-03"
CHANGES = [
    "Слой 2: нормализация + unified_cache",
    "feat: таблица unified_cache в БД + save_unified_data/get_unified_data",
    "feat: _parse_garmin_raw/_parse_coros_raw/_parse_polar_raw/_parse_strava_raw — мост между слоями 1.1 и 2",
    "feat: run_normalization() — читает raw_service_data, нормализует, мёрджит с приоритетами из DATA_NORMALIZER_SPEC, кладёт в unified_cache",
    "feat: scheduled_cache_refresh — garmin.fetch_raw/coros.fetch_raw/polar.fetch_raw/strava.fetch_raw + run_normalization после каждого юзера",
    "feat: _update_garmin_recovery_from_raw — парсит raw и обновляет garmin_recovery_cache (совместимость с _get_recovery_data)",
]
