# COROS Developer Support — письмо 1 (получено ≈ октябрь 2026, сохранено 22.10.2026)

Тема: Partner API — обновления для разработчиков, интересовавшихся Daily Data Push.

1. API Reference V2.1.1 (PDF в этой папке). Новое:
   - 5.3 — проверка подписи push-запросов (алгоритм и примеры кода)
   - 5.5 — Daily Data Push: сон, пульс покоя, HRV, шаги, калории
   - 6.3 — Training Workout Push: создание и обновление отдельных тренировок
   - 6.1 — поле deletedIds в ответах расписания
   На обложке старый номер версии; история правок на стр. 1 подтверждает V2.1.1.

2. Cheat sheet по подписи и Daily Data Push (PDF): SHA-1 (не HMAC-SHA256, как раньше говорила поддержка),
   пример с входами и ожидаемым результатом, поля Daily Data Push.
   sleepStartTime/sleepEndTime — время в постели; ppgHrv — RMSSD в мс.

3. Все изменения конфигурации — через форму (нужен client_id, без логина):
   https://coros-teams.feishu.cn/share/base/form/shrcnqJxUtOipq73pqjqf18osmg
   URL callback/webhook/health check, включение Daily Data Push и Workout Summary Push.
   Подтверждают по почте. Вопросы: developer@coros.com.

Если client_id ещё не выдан — письмо как справка до получения доступа.
