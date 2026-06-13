"""Разведка источников данных для будущего «пакета данных DeepSeek» — read-only.

Зачем: спроектировать сборщик данных для ИИ-анализа тренировки. Печатает, что реально
доступно в источниках, чтобы не гадать о схеме:
  1) Garmin splits — ВСЕ поля одного work-лэпа и одного recovery-лэпа последней
     DD-активности (HR/каденс/мощность/биомеханика — какие именно ключи есть);
  2) PROFILE — db.get_user_profile (только аналитически значимые поля; пароли/почты НЕ печатает);
  3) MORNING_SNAPSHOT — db.get_morning_caught (текущий замороженный утренний снимок);
  4) S4 — db.get_latest_workout_analysis('interval'): ключи analyzed_json + превью
     (есть ли там цель/суть/погода).

Ничего не строит и НЕ пишет в БД. Чувствительные данные не выводит.
Импортирует: garmin, database (оба без bot.py). НЕ импортирует bot.py.

Запуск (uid по умолчанию 2 = Anton):
    venv/bin/python3 scripts/probe_ai_data.py
    venv/bin/python3 scripts/probe_ai_data.py 6
"""
import sys
import os
import json
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import garmin
import database as db

# Поля профиля, релевантные анализу (без почт/паролей/токенов)
_PROFILE_WHITELIST = [
    "vo2max", "vo2max_source", "vo2max_updated_at",
    "lactate_threshold_pace", "lactate_threshold_hr", "lactate_source",
    "gender", "birthdate", "specialization", "updated_at",
]


def _short(v, n=80):
    s = repr(v)
    return s if len(s) <= n else s[:n] + "…"


async def probe_splits(uid):
    print("\n" + "=" * 70)
    print("1) GARMIN SPLITS — поля лэпов последней DD-активности")
    print("=" * 70)
    client = await garmin._client(uid)
    if not client:
        print(f"  нет клиента Garmin для uid={uid}")
        return
    acts = await asyncio.to_thread(client.get_activities, 0, 20)
    runs = [a for a in (acts or [])
            if "running" in str((a.get("activityType") or {}).get("typeKey", ""))
            and "DD_" in str(a.get("activityName") or "")]
    if not runs:
        print("  нет DD-беговой активности в последних 20")
        return
    act = runs[0]
    act_id = act.get("activityId")
    print(f"  Активность: {act.get('activityName')!r}  id={act_id}")

    splits = await asyncio.to_thread(client.get_activity_splits, act_id)
    if not isinstance(splits, dict):
        print(f"  splits не dict: {type(splits)}")
        return
    print(f"  splits top-level keys: {sorted(splits.keys())}")
    laps = splits.get("lapDTOs") or splits.get("laps") or []
    print(f"  лэпов: {len(laps)}")

    def _find(kind):
        for lp in laps:
            if str(lp.get("intensityType") or "").upper() == kind:
                return lp
        return None

    for kind, label in (("ACTIVE", "WORK-лэп"), ("RECOVERY", "RECOVERY-лэп")):
        lp = _find(kind)
        print(f"\n  --- {label} ({kind}) — все поля ---")
        if not lp:
            print("    (нет такого лэпа)")
            continue
        for k in sorted(lp.keys()):
            print(f"    {k:32} = {_short(lp[k])}")


def probe_db(uid):
    print("\n" + "=" * 70)
    print("2) PROFILE — get_user_profile (whitelist)")
    print("=" * 70)
    prof = db.get_user_profile(uid)
    if not prof:
        print("  профиль пуст")
    else:
        for k in _PROFILE_WHITELIST:
            if k in prof:
                print(f"  {k:24} = {_short(prof[k])}")

    print("\n" + "=" * 70)
    print("3) MORNING_SNAPSHOT — get_morning_caught (текущий снимок)")
    print("=" * 70)
    snap = db.get_morning_caught(uid)
    if not snap:
        print("  снимка нет")
    else:
        for k, v in snap.items():
            print(f"  {k:14} = {_short(v)}")

    print("\n" + "=" * 70)
    print("4) S4 — get_latest_workout_analysis('interval')")
    print("=" * 70)
    try:
        an, status = db.get_latest_workout_analysis("interval")
    except Exception as e:
        print(f"  ошибка: {type(e).__name__}: {e}")
        an, status = None, "err"
    if not an:
        print(f"  анализа нет (status={status})")
        return
    print(f"  status={status}  workout_date={an.get('workout_date')}  "
          f"mode={an.get('analysis_mode')}  post_id={an.get('post_id')}")
    aj_raw = an.get("analyzed_json")
    try:
        aj = json.loads(aj_raw) if aj_raw else {}
    except Exception as e:
        print(f"  analyzed_json не парсится: {e}")
        print(f"  превью raw: {_short(aj_raw, 300)}")
        return
    if isinstance(aj, dict):
        print(f"  analyzed_json keys: {sorted(aj.keys())}")
    print("  analyzed_json превью:")
    print("   " + json.dumps(aj, ensure_ascii=False, indent=1)[:1800])


async def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    await probe_splits(uid)
    probe_db(uid)


if __name__ == "__main__":
    asyncio.run(main())
