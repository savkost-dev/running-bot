VERSION = "0.22.0"
BUILD_DATE = "2026-06-03"
CHANGES = [
    "Polar fix + слой 1.1 (raw_service_data) + чистка вечерних промптов (HRV/BB)",
    "fix: Polar API был сломан (404) — исправлены пути /users/nightly-recharge и /users/sleep (без userId), реальные поля через подчёркивание, ANS charge→recovery 0-100",
    "feat: слой 1.1 — таблица raw_service_data + fetch_raw во всех сервисах (polar/garmin/coros/strava) тянут сырьё as is",
    "feat: COROS рабочий endpoint /dashboard/query (ltsp/lthr/recoveryPct/HRV/rhr)",
    "feat: data_normalizer.py — нормализатор слоя 2 (изолирован, прод не трогает)",
    "chore: убраны HRV и Body Battery из обоих вечерних промптов (build_evening_prompt + build_ai_b_prompt)",
    "fix: три фильтра против ложных анонсов — failsafe B (groups=[]→is_valid=false), промт C (явный запрет анонс-без-групп), день недели (long только вс weekday=6)",
]
