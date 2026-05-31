# -*- coding: utf-8 -*-
"""Sweep TAU from 24..30, print pct for every group at each value."""
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

import claude_advisor
from database import get_workout_analysis, get_user_profile, get_or_create_user
import zones as _zones

ADMIN_TG_ID = 273726778
POST_ID = 2004

row = get_workout_analysis(POST_ID)
analysis = json.loads(row["analyzed_json"])
db_uid  = get_or_create_user(ADMIN_TG_ID, "test")
profile = get_user_profile(db_uid)
zinfo   = _zones.get_pace_zones(db_uid)
user_data = {"db_user_id": db_uid, "specialization": (profile or {}).get("specialization"), "recovery": None}

print(f"{'TAU':>5}  {'гр1':>5}  {'гр2':>5}  {'гр3':>5}  {'гр4':>5}  {'гр5':>5}")
print("-" * 38)

for tau in (24, 25, 26, 27, 28, 29, 30):
    claude_advisor._TTT_TAU = float(tau)
    rec = claude_advisor.recommend_group(analysis, user_data)
    if not rec or not rec.get("ok"):
        print(f"  {tau}: FAIL")
        continue
    pcts = {g["number"]: g["pct"] for g in rec["groups"] if g.get("pct") is not None}
    print(f"{tau:>5}  {pcts.get('1','?'):>5}  {pcts.get('2','?'):>5}  "
          f"{pcts.get('3','?'):>5}  {pcts.get('4','?'):>5}  {pcts.get('5','?'):>5}")
