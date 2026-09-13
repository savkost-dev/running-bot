"""Разминка/заминка в эталоне Garmin (кейс DD_Long, 13.09.2026): что приходит в кругах, плане и точках.

Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/probe_warmup.py <uid> [селектор=DD_Long]
Печатает: сырые lapDTOs (шаг, интенсивность, дистанция, время, старт), шаги плана,
результат текущей разметки (_garmin_candidate → ordered/rows), число точек. Ничего не меняет.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import ai_package as ap  # noqa: E402
import activity_review as ar  # noqa: E402
from garmin import _load_token, _build_client  # noqa: E402


async def main():
    uid = int(sys.argv[1])
    sel = sys.argv[2] if len(sys.argv) > 2 else "DD_Long"
    client = _build_client(_load_token(uid))
    acts = client.get_activities(0, 10)
    act = next((a for a in acts if sel in (a.get("activityName") or "")), None)
    if not act:
        sys.exit("активность не найдена: " + ", ".join(str(a.get("activityName")) for a in acts))
    aid = act["activityId"]
    print("activity", aid, act.get("activityName"), "workoutId:", act.get("workoutId"))
    raw = client.get_activity_splits(aid)
    laps = raw.get("lapDTOs") or []
    print(f"\nСЫРЫЕ КРУГИ ({len(laps)}): ключи первого:", sorted(laps[0].keys())[:40] if laps else "-")
    for i, lp in enumerate(laps, 1):
        print(f"  {i:2}: step={lp.get('wktStepIndex')!s:>4} int={str(lp.get('intensityType')):<10} "
              f"dist={lp.get('distance')!s:>8} dur={lp.get('duration')!s:>7} start={lp.get('startTimeGMT')} "
              f"msg={lp.get('messageIndex')} lapIdx={lp.get('lapIndex')}")

    wid = act.get("workoutId")
    if wid:
        try:
            w = client.get_workout_by_id(wid)
            print("\nШАГИ ЗАДАНИЯ (верхний уровень):")
            for st in (w.get("workoutSegments") or [{}])[0].get("workoutSteps", []):
                stype = (st.get("stepType") or {}).get("stepTypeKey")
                if st.get("workoutSteps"):
                    print(f"  repeat x{st.get('numberOfIterations')}: " + ", ".join(
                        f"{(s.get('stepType') or {}).get('stepTypeKey')} {(s.get('endCondition') or {}).get('conditionTypeKey')}={s.get('endConditionValue')} "
                        f"target={(s.get('targetType') or {}).get('workoutTargetTypeKey')}"
                        for s in st["workoutSteps"]))
                else:
                    print(f"  {stype}: end={(st.get('endCondition') or {}).get('conditionTypeKey')}={st.get('endConditionValue')} "
                          f"target={(st.get('targetType') or {}).get('workoutTargetTypeKey')} "
                          f"{st.get('targetValueOne')}..{st.get('targetValueTwo')}")
        except Exception as e:  # noqa: BLE001
            print("workout по id не получен:", type(e).__name__, e)

    cand = await ap._garmin_candidate(uid, sel)
    if not cand:
        print("\n_garmin_candidate → None")
        return
    print("\nПЛАН (plan_steps):", [(p["idx"], p["stype"], p["dist"], p["bounds"]) for p in cand["plan_steps"]])
    ordered = ar._ordered_laps(cand["splits"])
    print("ordered:", [(l["step"], l["intensity"], l["dist"]) for l in ordered])
    rows, _ = ap._enrich_laps(cand["splits"], cand["plan_steps"], cand["pts"])
    print("rows:", [(r["label"], r["role"], r["step"], r["dist"]) for r in rows])
    print("pts:", len(cand["pts"] or []))


asyncio.run(main())
