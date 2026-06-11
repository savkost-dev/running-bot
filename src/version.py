VERSION = "0.24.79"
BUILD_DATE = "2026-06-11"
CHANGES = [
    "Расчётный Training Readiness для COROS/Strava в нормализаторе: "
    "base=clip((TSB+20)/0.4, 0..100); COROS = sqrt(base*RecoveryPct) (геом. среднее), "
    "COROS без Strava = RecoveryPct, Strava-only = base. Пишется в s3_training_readiness "
    "с level=coros-calc/strava-calc. Garmin/Polar не затронуты.",
    "0.24.78: альтернативы Легко/Тяжело только из групп с подходимостью ≥50%.",
    "0.24.77: сквозной логгер активности + /activity."
]
