VERSION = "0.24.55"
BUILD_DATE = "2026-06-07"
CHANGES = [
    "воскресный конвейер сдвинут позже (Garmin к утру не успевал обработать сон → BB/HRV/TR пустые): "
    "refresh 06:45→07:15 МСК, утренняя рассылка 07:00→07:30 МСК. Только воскресенье — вт/пт без изменений.",
    "scheduled_cache_refresh/scheduled_morning ограничены днями вт/пт через days=(1,4); "
    "добавлены scheduled_cache_refresh_sunday (07:15) и scheduled_morning_sunday (07:30) с days=(6,).",
]
