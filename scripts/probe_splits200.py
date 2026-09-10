"""Диагностика нарезки по 200 м: по каждому кругу — дистанция по точкам, куски, остаток.

Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/probe_splits200.py <db_user_id> [селектор]
Использует те же функции, что /report (ai_package), bot.py не импортирует.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import ai_package as ap  # noqa: E402


async def main():
    uid = int(sys.argv[1])
    sel = sys.argv[2] if len(sys.argv) > 2 else None
    cand = await ap._garmin_candidate(uid, ap._expand_selector(sel))
    if not cand:
        sys.exit("нет Garmin-кандидата")
    splits, pts = cand["splits"], cand["pts"]
    laps = [l for l in (splits.get("lapDTOs") or []) if isinstance(l, dict)]
    starts = [ap._gmt_ms(l.get("startTimeGMT")) for l in laps]
    print("points:", len(pts or []), "laps:", len(laps))
    for n, lp in enumerate(laps):
        d, t = lp.get("distance"), lp.get("duration")
        s_ms = starts[n]
        e_ms = starts[n + 1] if n + 1 < len(starts) else (s_ms + int((t or 0) * 1000) if s_ms else None)
        seg = [p for p in (pts or []) if p[0] is not None and s_ms and e_ms and s_ms <= p[0] < e_ms and p[1] is not None]
        covered = (seg[-1][1] - seg[0][1]) if len(seg) >= 2 else None
        sp = ap._splits_200(pts, s_ms, e_ms, d) if (pts and s_ms and e_ms) else None
        rem = (covered - 200 * (len(sp) - 1)) if (sp and covered is not None) else None
        print(f"lap {n+1}: step={lp.get('wktStepIndex')} lap_dist={d} pts={len(seg)} covered={covered and round(covered,1)} "
              f"chunks={len(sp) if sp else 0} last_rem≈{rem and round(rem,1)} sp={sp}")


asyncio.run(main())
