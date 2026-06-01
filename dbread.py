import sqlite3, glob, json, os
os.chdir('/opt/running-bot')
dbs = glob.glob('*.db') + glob.glob('src/*.db') + glob.glob('data/*.db')
db = dbs[0] if dbs else None
print('DB:', db, '| files:', os.listdir('.'))
if db:
    c = sqlite3.connect(db)
    row = c.execute("select workout_date, analyzed_json from workout_analysis where workout_type='interval' order by created_at desc limit 1").fetchone()
    print('DATE:', row[0])
    d = json.loads(row[1])
    print('STRUCTURE:', json.dumps(d.get('structure'), ensure_ascii=False))
    print('GROUPS:', json.dumps(d.get('groups'), ensure_ascii=False, indent=1))
    print('EXTRA:', json.dumps(d.get('extra_groups'), ensure_ascii=False))
    print('borderline:', d.get('is_borderline'))