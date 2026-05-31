VERSION = "0.19.2"
BUILD_DATE = "2026-05-31"
CHANGES = [
    "fix: единый общий Telethon-клиент на процесс — устраняет 'database is locked' при пересечении вызовов",
    "fix: убран паттерн async with get_client() на каждый вызов (несколько клиентов на один .session)",
    "feat: connect_client при старте + close_client при остановке; ленивое переподключение при обрыве",
]
