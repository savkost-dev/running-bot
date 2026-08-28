import sqlite3, json, sys
sys.path.insert(0, "/opt/running-bot/src")
from fit_generator import build_garmin_from_analysis
con = sqlite3.connect("/opt/running-bot/running_bot.db")
q = "SELECT analyzed_json FROM workout_analysis WHERE workout_date=? AND is_valid=1 ORDER BY id DESC LIMIT 1"
r = con.execute(q, ("2026-08-25",)).fetchone()
a = json.loads(r[0])
def walk(steps, pad=""):
    for st in steps:
        if st.get("type") == "RepeatGroupDTO":
            n = st.get("numberOfIterations")
            sk = st.get("skipLastRestStep")
            print(pad + "REPEAT x" + str(n) + " skipLastRest=" + str(sk))
            walk(st.get("workoutSteps") or [], pad + "  ")
        else:
            t = (st.get("stepType") or {}).get("stepTypeKey")
            d = st.get("endConditionValue")
            print(pad + str(t) + ": " + str(d) + " m")
for g in ("3", "3.5"):
    print("=== группа " + g)
    wj = build_garmin_from_analysis(a, g)
    segs = wj.get("workoutSegments") or []
    steps = segs[0].get("workoutSteps") if segs else (wj.get("workoutSteps") or [])
    walk(steps or [])
