# DoDick — Спецификация нормализатора данных

> Статус: проектирование (02.06.2026). Не влияет на прод до явного переключения.

## Цель

Единый слой нормализации между сервисами и промптом.
Каждый сервис → свой normalizer → UnifiedUserData → промпт.
Промпт не знает откуда данные.

## Типы источников

- **Сервис** — данные пришли из API (Garmin, COROS, Polar, Strava)
- **Manual** — пользователь ввёл вручную

## Выходной формат UnifiedUserData

```python
@dataclass
class UnifiedUserData:
    # --- Форма ---
    lactate_threshold_pace: str | None    # "4:04" (мин:сек/км)
    lactate_threshold_hr:   int | None    # уд/мин
    vo2max:                 float | None  # мл/кг/мин
    zones: dict | None                    # {"easy": "5:30", "threshold": "4:04", ...}

    # --- Восстановление ---
    recovery_daily: int | None            # 0–100, суточное (COROS recoveryPct, Polar ANS, Whoop)
    recovery_total: float | None          # TSB — длительная усталость (+ свежий, - устал)
    readiness:      int | None            # 0–100, готовность (пока только Garmin TR)
    hrv:            float | None          # мс, ночной
    hrv_baseline:   float | None          # 7-дневная база
    rhr:            int | None            # ЧСС покоя

    # --- Нагрузка ---
    load_48h: dict | None                 # sessions, km, hours_ago, intensity
    training_load: dict | None            # ctl, atl, tsb, summary

    # --- Мета ---
    sources: list[str]                    # ["garmin", "strava"]
    updated_at: str                       # ISO timestamp
```

---

## Три категории восстановления

| Поле | Смысл | Период | Источники |
|---|---|---|---|
| `recovery_daily` | Как восстановился за эту ночь | Суточное | COROS, Polar, Whoop |
| `recovery_total` | Накопилась ли усталость за недели | Длительное (TSB) | Strava, Garmin, COROS |
| `readiness` | Готов ли к тренировке (суточное + длительное) | Комбинированное | Garmin TR |

> Polar закрывает только суточное. Для длительного пользователю с Polar нужна Strava.

## Таблица мэппинга источников → s3_ поля

Легенда покрытия:
- 🟢 зелёный — все три основных сервиса (Garmin + COROS + Polar)
- 🟡 жёлтый — сервис + Strava (агрегатор) или + Whoop
- ⚪ без фона — частичное покрытие

Тип: **прямой** = поле берётся как есть; **расчёт** = вычисляется по формуле.

### Тренированность

| Поле | Garmin | COROS | Polar | Strava | Покрытие |
|---|---|---|---|---|---|
| `s3_vo2max` | прямой: `vo2max` | расчёт: `_vdot_from_ltsp(ltsp)` | прямой: `aerobicFitness` | расчёт: `_vdot_from_predictions()` | 🟢 |
| `s3_lactate_threshold_pace` | прямой: `lactate_threshold_pace` | расчёт: `_sec_to_pace(ltsp)` | — | — | ⚪ |
| `s3_lactate_threshold_hr` | прямой: `lactate_threshold_hr` | прямой: `lthr` | — | — | ⚪ |
| `s3_zones` | расчёт: zones.py из ЛП/VO2max | расчёт: zones.py из ltsp | расчёт: zones.py из VO2max | расчёт: zones.py из VDOT | 🟢 |

### Восстановление

| Поле | Garmin | COROS | Polar | Strava | Покрытие |
|---|---|---|---|---|---|
| `s3_recovery_daily` | прямой: `body_battery` (отключён в промпте) | прямой: `recoveryPct` (суточный 0–100) | расчёт: `(ans_charge+10)/20×100` | — | 🟢 |
| `s3_recovery_total` | прямой: Training Readiness (0–100) | расчёт: `ati`/`cti` (когда API заработает) | — | расчёт: `TSB = CTL − ATL` (шкала ±) | 🟡 |
| `s3_hrv` | прямой: `hrv_last_night` | прямой: `avgSleepHrv` | прямой: `hrv_rmssd` | — | 🟢 |
| `s3_hrv_baseline` | прямой: `hrv_weekly_avg` | прямой: `sleepHrvBase` | — | — | ⚪ |
| `s3_rhr` | прямой: `rhr` | прямой: `rhr` | прямой: `heart-rate-avg` | — | 🟢 |

### Нагрузка

| Поле | Garmin | COROS | Polar | Strava | Покрытие |
|---|---|---|---|---|---|
| `s3_load_recent` | прямой: `get_activities_48h()` | прямой: `/activity/query` 48ч | — | прямой: `load_48h` | 🟡 |
| `s3_load_chronic` | прямой: `training_load` (своя метрика) | — (`ati`/`cti` endpoint 500) | — | прямой: `CTL/ATL/TSB` (эталон) | 🟡 |

### Важные заметки по мэппингу

- **`recoveryPct` (COROS)** — суточная метрика (есть значение на каждый день, шкала 0–100: 0–19 истощён, 20–69 уставший, 70–89 хороший, 90–100 отличный). Идёт ТОЛЬКО в `s3_recovery_daily`, НЕ в `recovery_total`.
- **Body Battery (Garmin)** — суточная, идёт в `s3_recovery_daily`, но сейчас отключена в промпте (меняется в течение дня, занижает вечером).
- **`s3_recovery_total` несовместимость шкал** — Garmin TR и COROS дают 0–100, Strava TSB даёт ±число. Объединять с осторожностью (открытый вопрос для слоя потребления).
- **Polar** закрывает только суточное восстановление + VO2max + HRV. Для длительной нагрузки нужна Strava.
- **COROS `ati`/`cti`** — острая/хроническая нагрузка EvoLab, endpoint пока даёт 500. Добавить когда официальный API одобрят.

## Отложенная метрика: readiness (готовность)

> ⏸ ВАЖНАЯ метрика, отложена. Вернёмся к ней позже.

Готовность к тренировке — потенциально ключевая метрика, но пока вызывает путаницу, поэтому отложена.

Проблема разграничения:
- **Garmin Training Readiness** (0–100) — готовность НА СЕГОДНЯ, меняется каждый день. Учитывает сон, HRV, нагрузку, восстановление.
- **COROS «Беговая форма»** (89.9, aerobicEnduranceScore) — уровень ТРЕНИРОВАННОСТИ, меняется медленно. По смыслу ближе к VO2max, а не к суточной готовности.

Это РАЗНЫЕ вещи, их нельзя класть в одно поле:
- «готов ли я сегодня» (Garmin TR) — оперативная метрика
- «какой я спортсмен» (COROS форма, VO2max) — фоновая метрика

Есть мысли по объединению/разделению, но пока поле readiness только мешает. Источники когда вернёмся:
- Garmin: Training Readiness score
- COROS: беговая форма / EvoLab скоры (уже тащим в get_dashboard_data)
- Polar: нет
- Strava: TSB косвенно

## Приоритет при использовании (не при сборе)

> Этот раздел относится к слою потребления (промпт/рекомендация), не к нормализатору.
> Нормализатор собирает и хранит всё. Приоритизация — отдельный вопрос.

Manual всегда выше сервиса (пользователь знает своё тело, плюс `vo2max_locked`/`lactate_locked`).

```python
SOURCE_PRIORITY = {
    "lactate_threshold_pace": ["manual", "garmin", "coros"],
    "lactate_threshold_hr":   ["manual", "garmin", "coros"],
    "vo2max":                 ["manual", "garmin", "coros", "polar", "strava"],
    "zones":                  ["manual", "garmin", "coros", "polar", "strava"],
    "recovery_score":         ["coros", "polar"],
    "hrv":                    ["garmin", "coros", "polar"],
    "hrv_baseline":           ["garmin", "coros"],
    "rhr":                    ["garmin", "coros", "polar"],
    "load_48h":               ["garmin", "coros", "strava"],
    "training_load":          ["strava", "garmin", "coros"],
}
```

---

## Что НЕ входит в UnifiedUserData

| Поле | Сервис | Причина |
|---|---|---|
| `body_battery` | Garmin | Меняется в течение дня, не показатель восстановления |
| `training_readiness` | Garmin | Только Garmin, смещение при частичной доступности |
| `sleep_hours` / `sleep_score` | Whoop, Polar | Whoop низкий приоритет |
| `stamina_level`, EvoLab scores | COROS | Специфика COROS |
| `recovery_time_h` | COROS | Специфика COROS |
| `ans_rate` | Polar | Специфика Polar |
| `suffer_score` | Strava | Только для расчёта intensity внутри load_48h |
| `predictions` | Strava | Только внутри нормализатора для расчёта VDOT |

---

## Архитектура — четыре слоя

```
Слой 1.1 — Fetch
  garmin.py / coros.py / polar.py / strava.py
  Тянут сырые данные из API, сохраняют raw_json в БД без изменений.

Слой 1.2 — Enrich  (постепенно, по мере необходимости)
  Вычисление производных метрик из открытых формул:
  - VDOT из ЛП или прогнозов → зоны по Дэниелсу (уже в zones.py)
  - TSB = CTL − ATL (если сервис отдаёт оба)
  - Нормализация шкал (Polar ANS charge -10..+10 → 0–100)
  - HRV отклонение: (hrv_last − hrv_baseline) / hrv_baseline × 100
  НЕ воспроизводим проприетарные алгоритмы (Nightly Recharge, HRV Status,
  Training Readiness, COROS recoveryPct) — они закрыты, берём as is.

Слой 2 — Normalize  (data_normalizer.py)
  raw_json / enriched_json → UnifiedUserData
  Мэппинг в единый формат. Запускается сразу после fetch.
  Сохраняет unified_json в unified_cache.
  Не знает про замки и ручные данные.

Слой 3 — Consume  (claude_advisor.py, bot.py)
  Читает UnifiedUserData из unified_cache.
  Не знает какой сервис был источником.

Слой 4 — Profile override
  Ручные данные пользователя + замки (vo2max_locked, lactate_locked).
  Переопределяет поля из слоя 2 при чтении в слое 3.
  Логика: если поле locked → брать из профиля, иначе из unified_cache.
  Находится между слоями 2 и 3, реализуется отдельной функцией get_unified_data().
```

## Функции нормализатора

```python
data_normalizer.py

  normalize_garmin(raw: dict) -> UnifiedUserData
  normalize_coros(raw: dict) -> UnifiedUserData
  normalize_polar(raw: dict) -> UnifiedUserData
  normalize_strava(raw: dict) -> UnifiedUserData
  merge(parts: list[UnifiedUserData]) -> UnifiedUserData  # объединяет данные нескольких сервисов
```

## Новая таблица БД: unified_cache

```sql
CREATE TABLE unified_cache (
    user_id       INTEGER PRIMARY KEY REFERENCES users(id),
    unified_json  TEXT NOT NULL,
    raw_json      TEXT,
    sources       TEXT,
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

## Чеклист

- [ ] data_normalizer.py
- [ ] unified_cache в database.py
- [ ] Сервисные файлы сохраняют raw_json при fetch
- [ ] bot.py/claude_advisor.py переключаются на unified_cache
- [ ] Whoop — не трогать
