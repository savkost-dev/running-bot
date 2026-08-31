import sqlite3, json, sys
sys.path.insert(0, "/opt/running-bot/src")
from fit_generator import build_garmin_from_analysis
con = sqlite3.connect("/opt/running-bot/running_bot.db")
def walk(steps, pad=""):
    for st in steps:
        if st.get("type") == "RepeatGroupDTO":
            print(pad + "REPEAT x" + str(st.get("numberOfIterations")) + " skipLast=" + str(st.get("skipLastRestStep")))
            walk(st.get("workoutSteps") or [], pad + "  ")
        else:
            print(pad + str((st.get("stepType") or {}).get("stepTypeKey")) + ": " + str(st.get("endConditionValue")))
for date in ("2026-09-01", "2026-08-28", "2026-08-25"):
    q = "SELECT analyzed_json FROM workout_analysis WHERE workout_date=? AND is_valid=1 ORDER BY id DESC LIMIT 1"
    r = con.execute(q, (date,)).fetchone()
    if not r: 
        print("=== " + date + ": нет"); continue
    a = json.loads(r[0])
    print("=== " + date + " группа 3.5")
    wj = build_garmin_from_analysis(a, "3.5")
    segs = wj.get("workoutSegments") or []
    walk((segs[0].get("workoutSteps") if segs else wj.get("workoutSteps")) or [])
