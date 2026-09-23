# Эталоны для Garmin — памятка по JSON тренировки (22.09.2026)

Источник: README проекта Taxuspt/garmin_mcp (https://github.com/taxuspt/garmin_mcp) — MCP-сервер поверх той же библиотеки python-garminconnect, которой пользуется наш `garmin.py`. Проверено ими на боевом API Garmin; у нас НЕ перепроверялось.

## Главное правило
Garmin считает истиной числовой id, а не текстовый ключ. Если id и ключ расходятся, сохраняется то, что соответствует id, — молча, без ошибки.

## Условия завершения шага (endCondition)
| id | ключ |
|----|------|
| 1 | lap.button |
| 2 | time |
| 3 | distance |
| 4 | calories |
| 5 | power |
| 6 | heart.rate |
| 7 | iterations |
| 8 | fixed.rest |
| 9 | fixed.repetition |
| 10 | reps |
| 11 | training.peaks.tss |

Частая ошибка: пульсовое условие с id 4 — это калории, нужен id 6.

## Цели шага (targetType)
- id 4 — heart.rate.zone: свой диапазон пульса в ударах кладётся в `targetValueOne` / `targetValueTwo`; именованная зона Garmin — через `zoneNumber` (1–5). Вместе их не слать: Garmin оставит зону и выбросит диапазон.
- id 6 — pace.zone: границы темпа в метрах в секунду (например 8:00–8:30/км = 1.9607843 и 2.0833333).
- `targetValueOne` / `targetValueTwo` кладутся на сам шаг, рядом с `targetType`, а НЕ внутрь объекта `targetType` — вложенные значения Garmin молча выбрасывает, и цель остаётся без диапазона.
- Пульсовое условие завершения по зоне: `endConditionZone` (1–5) на объекте `endCondition`, без `endConditionValue`.

## Прочее оттуда же
- `get_activity_details` они намеренно не используют — ответ большой (50–500 КБ). Мы им пользуемся в разборе осознанно: нужен посекундный ряд.
- Скачать файл тренировки можно в fit / gpx / tcx / csv.
