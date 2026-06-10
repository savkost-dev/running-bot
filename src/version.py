VERSION = "0.24.77"
BUILD_DATE = "2026-06-10"
CHANGES = [
    "Активность пользователей: сквозной логгер (group=-1) пишет в user_activity все команды "
    "и inline-кнопки (btn:<data>); кнопки Тренировка/Long Run/Утро считаются как "
    "/workout|/long|/morning (точечные log_activity убраны — без дублей). "
    "Новая админ-команда /activity: по дням МСК + топ действий за 14 дней, без рассылок.",
    "0.24.76: фикс +3ч в _recovery_scenario (wellnessEndTimeLocal уже МСК).",
    "0.24.75: failsafe Шага 1 — невалидно без групп с темпами (прогрев-посты)."
]
