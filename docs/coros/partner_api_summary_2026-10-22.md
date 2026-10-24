# COROS Partner API V2.1.1 — что это для бота (разбор 22.10.2026)

Источники: `COROS API Reference V2.1.1.pdf` (актуальный, из второго письма), `COROS_Partner_API_Signature_Cheat_Sheet.pdf`,
письма `2026-10_letter1_*.md`, `2026-10_letter2_*.md`.

## Что это
Третий путь к данным COROS, отдельный от MCP (`mcpeu.coros.com`, модуль `coros_mcp.py`) и от старого COROS
по паролю (`teamapi.coros.com`, модуль `coros.py`). База `https://open.coros.com`. Работает только с `client_id`
и `client_secret`, которые COROS выдаёт после заявки. Из писем не видно, выданы ли они Антону.
Авторизация пользователей — свой OAuth (код → accessToken на 30 дней с refresh), свой `openId`;
подключения MCP не переносятся. Запросы в прошлое — не глубже 3 месяцев, окно одного запроса — 30 дней.

## Что даёт по сравнению с MCP
- **Push вместо опроса.** COROS сам шлёт на наш HTTPS-адрес итог каждой тренировки (5.4, с `fitUrl` на FIT)
  и раз в день сон, пульс покоя, HRV, шаги, калории за последние три дня (5.5). Дневной лимит на FIT не упомянут.
- **Данных меньше.** Дневная сводка: время в постели (`sleepStartTime/sleepEndTime` — не сон и не фазы),
  `rhr`, `ppgHrv` (RMSSD, мс, ночное среднее), `hrvList` (сырой ряд за день), `sleepAvgHr`, шаги, калории.
  Готовности, нагрузки, стресса, порогов нет — это только в MCP.
- **Загрузка тренировок богаче (6.1 расписание, 6.3 одна тренировка).** Структура: `Repetition` с вложенными
  `Steps`, `IntensityClass` WarmUp/Active/Rest/CoolDown, `Length` в Meter/Second/EndManually, отдельное поле
  `Rest` (Second/Bpm/EndManually). Можно обновлять по своему `Id` и удалять расписание (6.2) — в MCP нельзя.
  Темп только процентом от порога (`PercentOfThresholdSpeed`, `RangeOfThresholdSpeed`), но `ThresholdSpeed`
  (м/с) пишется в каждом шаге → абсолютный темп через «порог = наш темп, 100%». Пропуска последнего отдыха нет.
  Лимит: 30 тренировок за один push, горизонт 1 год.
- Поле `isGray` в userinfo — пользователь в белом списке COROS.

## Что нужно, чтобы включить
1. HTTPS-адрес для приёма push + GET «жив» (200). У нас есть `oauth_server.py`, можно поднять на нём.
   Один адрес на оба push, различать по телу (`sportDataList` vs `batchDailyList`).
2. Наш токен 16–32 символа для подписи (регистрируется через форму вместе с URL).
   Проверка: `sha1("".join(sorted([token, timestamp, nonce])))` == параметр `signature` в URL;
   плюс сверка заголовков `client` и `secret`. Пример из шпаргалки:
   token `s1ntjN9GkiMKeygoH3r84pC2dc05TvWI`, timestamp `1715428513`, nonce `321291201` →
   `f542618b60ce0211349a7a0d5edfd347ef903b5a`. Это SHA-1, не HMAC-SHA256 (поправка COROS).
3. Отвечать `{"result":"0000","message":"ok"}`; иначе COROS повторяет сутки и бросает.
4. Конфигурация (URL, включение push) — через форму, нужен `client_id`:
   https://coros-teams.feishu.cn/share/base/form/shrcnqJxUtOipq73pqjqf18osmg . Вопросы: developer@coros.com.

## Вывод
Для рекомендаций MCP остаётся основным, Partner API его не заменяет (нет готовности и нагрузки).
Интересен для двух вещей: разбор сразу после тренировки без опроса и загрузка эталонов с правкой и удалением.
Браться, когда придёт `client_id`.
