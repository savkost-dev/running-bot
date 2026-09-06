"""Кто в единой базе сегодня с низким восстановлением (recovery_score < 40)."""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from database import get_connection  # noqa: E402

conn = get_connection()
cur = conn.cursor()
tabs = [r[0] for r in cur.execute("select name from sqlite_master where type='table' and name like '%unified%'")]
print("таблицы:", tabs)
names = {}
try:
    ucols = [r[1] for r in cur.execute("pragma table_info(users)")]
    print("users cols:", ucols)
    for r in cur.execute("select * from users where id in (25,37,47)"):
        print("   ", dict(zip(ucols, r)))
    for r in cur.execute("select id, username from users"):
        names[r[0]] = f"@{r[1]}" if r[1] else ""
except Exception as e:
    print("users:", e)
for t in tabs:
    cols = [r[1] for r in cur.execute(f"pragma table_info({t})")]
    jcol = next((c for c in cols if "json" in c), None)
    ucol = next((c for c in cols if "user" in c), None)
    tcol = next((c for c in cols if "updated" in c or "created" in c), None)
    print(t, cols)
    if not jcol or not ucol:
        continue
    out = []
    for u, j, ts in cur.execute(f"select {ucol}, {jcol}, {tcol or 'NULL'} from {t}").fetchall():
        try:
            d = json.loads(j)
        except Exception:
            continue
        rs = d.get("s3_recovery_daily")
        if rs is not None and float(rs) < 40:
            out.append((u, names.get(u, ""), rs, d.get("s3_training_readiness"), ts))
    print("низкое восстановление (<40):")
    for o in sorted(out, key=lambda x: str(x[4] or ""), reverse=True)[:15]:
        print("  ", o)
