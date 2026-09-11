"""Диагностика моделей графика для одного источника: круги ordered vs x_ticks из _segment_model.

Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/probe_chart_model.py <db_user_id> <strava|garmin|coros> [селектор]
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import ai_package as ap  # noqa: E402
import activity_review as ar  # noqa: E402


async def main():
    uid, src = int(sys.argv[1]), sys.argv[2]
    sel = ap._expand_selector(sys.argv[3] if len(sys.argv) > 3 else None)
    fn = {"strava": ap._strava_candidate, "garmin": ap._garmin_candidate, "coros": ap._coros_candidate}[src]
    cand = await fn(uid, sel)
    if not cand:
        sys.exit("кандидата нет")
    splits, plan_steps, pts = cand["splits"], cand["plan_steps"], cand["pts"]
    laps = [l for l in (splits.get("lapDTOs") or []) if isinstance(l, dict)]
    print("pts:", len(pts or []), "laps:", len(laps))
    for l in laps:
        print("  lap step=%s int=%s dist=%s dur=%s start=%s" % (
            l.get("wktStepIndex"), l.get("intensityType"), l.get("distance"), l.get("duration"), l.get("startTimeGMT")))
    print("plan:", [(p["idx"], p["stype"], p["dist"]) for p in plan_steps])
    ordered = ar._ordered_laps(splits)
    print("ordered:", [(l["step"], l["intensity"], l["dist"]) for l in ordered])
    work_roles, x_ticks, rest_paces, S = ar._segment_model(ordered, plan_steps)
    print("x_ticks:", x_ticks)
    print("work_roles xs:", [(r["label"], r["xs"]) for r in work_roles])
    rows, _ = ap._enrich_laps(splits, plan_steps, pts)
    print("rows splits200:", [(r["label"], r["role"], r.get("splits200")) for r in rows])
    if pts:
        print("pts t0=%s t_end=%s d_end=%s" % (pts[0][0], pts[-1][0], pts[-1][1]))
        starts = [ap._gmt_ms(l.get("startTimeGMT")) for l in laps]
        for n, l in enumerate(laps):
            s_ms = starts[n]
            e_ms = starts[n + 1] if n + 1 < len(starts) else None
            seg = [p for p in pts if s_ms and e_ms and s_ms <= p[0] < e_ms]
            cov = (seg[-1][1] - seg[0][1]) if len(seg) >= 2 else None
            print("  lap %d: start_ms=%s end_ms=%s in_window=%d covered=%s lap_dist=%s" % (
                n + 1, s_ms, e_ms, len(seg), cov, l.get("distance")))


asyncio.run(main())
