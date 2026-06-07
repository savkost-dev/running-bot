VERSION = "0.24.62"
BUILD_DATE = "2026-06-07"
CHANGES = [
    "Снимок «на утро» Garmin TR: берём первую запись training_readiness ПОСЛЕ "
    "пробуждения (timestampLocal >= sleepEndTimestampLocal) — гарантированно после сна "
    "и свежая, не дневная сползшая. Фолбэк — самая ранняя запись если пробуждение неизвестно.",
]
