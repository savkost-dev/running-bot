VERSION = "0.24.76"
BUILD_DATE = "2026-06-10"
CHANGES = [
    "Фикс +3ч в _recovery_scenario: data_fetched_at для Garmin = wellnessEndTimeLocal (уже МСК), "
    "конвертация добавляла лишние 3 часа в текст рекомендации и в расчёт hours_until. "
    "naive-время теперь считается МСК, aware (Z) конвертируется как раньше.",
    "0.24.75: failsafe Шага 1 — невалидно без групп с темпами (прогрев-посты).",
    "0.24.74: Garmin fetch_raw — проверка полноты ночи перед записью сырья."
]
