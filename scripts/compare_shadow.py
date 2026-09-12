"""Матрица миграций между двумя результатами за дату.

Запуск на сервере: cd /opt/running-bot && venv/bin/python scripts/compare_shadow.py <дата> <стало> [было]
  стало — run_kind из recommendation_history (shadow, shadow_1, mailing…)
  было — run_kind из истории; без него — last_recommendation за дату (рассылка до появления истории)
Примеры: compare_shadow.py 2026-09-11 shadow          (рассылка → shadow)
         compare_shadow.py 2026-09-11 shadow_1 shadow (shadow → shadow_1: разброс одного режима)
Печатает матрицу «было × стало», совпадения/соседние/дальше и разошедшихся построчно. Ничего не меняет.
"""
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import database as db  # noqa: E402

date = sys.argv[1]
kind = sys.argv[2] if len(sys.argv) > 2 else "shadow"
base = sys.argv[3] if len(sys.argv) > 3 else None
ORDER = ["1", "2", "3", "3.5", "4", "5", "6"]


def _norm(g):
    g = str(g or "").replace(",", ".").strip()
    return g[:-2] if g.endswith(".0") else g


with db.get_connection() as conn:
    if base:
        was = {r[0]: (_norm(r[1]), r[2], r[3]) for r in conn.execute(
            "SELECT user_id, recommended_group, ai_mode, groups_pct FROM recommendation_history "
            "WHERE workout_date = ? AND run_kind = ?", (date, base))}
    else:
        was = {r[0]: (_norm(r[1]), r[2], None) for r in conn.execute(
            "SELECT user_id, recommended_group, ai_mode FROM last_recommendation WHERE workout_date = ?", (date,))}
    now = {r[0]: (_norm(r[1]), r[2], r[3], r[4]) for r in conn.execute(
        "SELECT h.user_id, h.recommended_group, h.ai_mode, h.groups_pct, u.name "
        "FROM recommendation_history h LEFT JOIN users u ON u.id = h.user_id "
        "WHERE h.workout_date = ? AND h.run_kind = ?", (date, kind))}

both = sorted(set(was) & set(now))
print(f"дата {date}: было ({base or 'рассылка'}) {len(was)}, стало ({kind}) {len(now)}, пересечение {len(both)}")
mat = defaultdict(Counter)
same = adj = far = 0
diff_rows = []
for uid in both:
    a, b = was[uid][0], now[uid][0]
    mat[a][b] += 1
    if a == b:
        same += 1
        continue
    ia, ib = (ORDER.index(a) if a in ORDER else -1), (ORDER.index(b) if b in ORDER else -1)
    step = abs(ia - ib) if ia >= 0 and ib >= 0 else 9
    adj += step == 1
    far += step > 1
    pct = {}
    try:
        pct = json.loads(now[uid][2] or "{}")
    except ValueError:
        pass
    pct0 = {}
    try:
        pct0 = json.loads(was[uid][2] or "{}")
    except (ValueError, TypeError):
        pass
    diff_rows.append((step, uid, now[uid][3], a, was[uid][1], b,
                      pct0.get(a), pct.get(a), pct0.get(b), pct.get(b)))

cols = [g for g in ORDER if any(mat[a][g] for a in mat)] or ORDER
print("\nМатрица (строки — было/" + (base or "рассылка") + ", столбцы — стало/" + kind + "):")
print("было\\стало " + " ".join(f"{g:>5}" for g in cols) + "   всего")
for a in ORDER:
    if a not in mat:
        continue
    print(f"{a:>10} " + " ".join(f"{mat[a][g] or '·':>5}" for g in cols) + f"   {sum(mat[a].values()):>5}")
print(f"\nсовпало {same} · соседняя группа {adj} · дальше {far}  (из {len(both)})")
print("\nРазошлись: было → стало; в скобках процент каждой группы в первом → во втором прогоне («?» — нет данных):")


def _p(v):
    return "?" if v is None else f"{v}%"


for step, uid, name, a, mode_was, b, a0, a1, b0, b1 in sorted(diff_rows, key=lambda r: (-r[0], r[3])):
    print(f"  uid={uid:<4} {str(name)[:22]:<22} гр{a:>4} → гр{b:<4}  "
          f"гр{a}: {_p(a0)} → {_p(a1)}   гр{b}: {_p(b0)} → {_p(b1)}")
