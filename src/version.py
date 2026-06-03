VERSION = "0.23.0"
BUILD_DATE = "2026-06-03"
CHANGES = [
    "COROS EU-регион fix + Garmin авто-релогин + пол/ДР в user_profile",
    "fix: COROS EU-юзеры не получали данные (хардкод teamapi) — автоопределение teamapi/teameuapi + кэш в памяти и колонка coros_region. Ловим result:1019 (invalid token с HTTP 200)",
    "fix: Garmin протухшие токены — авто-релогин в _client через сохранённые credentials (_reauth по образцу COROS)",
    "feat: пол + дата рождения в user_profile (колонки gender/birthdate, статичны). get_profile во всех сервисах (единый формат male/female + YYYY-MM-DD), backfill_profile.py разовый. Заполнено 13 юзеров",
    "confirmed: COROS sex 0=муж, 1=жен (подтверждено на Karpov/Истоминой)",
    "Polar fix + слой 1.1 (raw_service_data) + чистка вечерних промптов (HRV/BB)",
]
