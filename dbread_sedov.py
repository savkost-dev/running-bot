import sqlite3, glob, json, os
# Запускать на сервере: ssh ... 'cd /opt/running-bot && venv/bin/python3 - < dbread_sedov.py'
# (или локально, если БД свежая)
for base in ('/opt/running-bot', '.'):
    if os.path.isdir(base):
        os.chdir(base); break
dbs = glob.glob('*.db') + glob.glob('src/*.db') + glob.glob('data/*.db')
db = dbs[0] if dbs else None
print('DB:', os.path.abspath(db) if db else None)
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

def cols(table):
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]

# ── 1. Найти Седова ───────────────────────────────────────────
rows = c.execute(
    "SELECT id, telegram_id, name, username FROM users "
    "WHERE lower(name) LIKE '%седов%' OR lower(name) LIKE '%sedov%' "
    "   OR lower(username) LIKE '%sedov%' OR lower(username) LIKE '%седов%'"
).fetchall()
print('\n=== КАНДИДАТЫ ===')
for r in rows:
    print(dict(r))
if not rows:
    print('Седов не найден по LIKE — впиши db_user_id вручную ниже')
uid = rows[0]['id'] if rows else None
print('Используем db_user_id =', uid)

if uid:
    # ── 2. Профиль ────────────────────────────────────────────
    p = c.execute(
        "SELECT vo2max, lactate_threshold_pace, lactate_threshold_hr, specialization, gender "
        "FROM user_profile WHERE user_id = ?", (uid,)
    ).fetchone()
    print('\n=== ПРОФИЛЬ ===')
    print(dict(p) if p else 'нет профиля')

    # ── 3. Зоны ───────────────────────────────────────────────
    z = c.execute(
        "SELECT pace_zones_json, zones_source, zones_updated_at FROM athlete_cache WHERE user_id = ?",
        (uid,)
    ).fetchone()
    print('\n=== ЗОНЫ ===')
    if z and z['pace_zones_json']:
        print('source:', z['zones_source'], '| updated:', z['zones_updated_at'])
        print(json.dumps(json.loads(z['pace_zones_json']), ensure_ascii=False, indent=1))
    else:
        print('нет зон в athlete_cache')

    # ── 4. last_recommendation (лонг может не сохраняться) ─────
    lr = c.execute("SELECT * FROM last_recommendation WHERE user_id = ?", (uid,)).fetchone()
    print('\n=== last_recommendation ===')
    print(dict(lr) if lr else 'пусто (лонг не пишется в эту таблицу)')

    # ── 5. Оценки Седова ──────────────────────────────────────
    rt = c.execute(
        "SELECT rating, ai_mode, comment, workout_date, created_at "
        "FROM recommendation_ratings WHERE user_id = ? ORDER BY created_at DESC LIMIT 5", (uid,)
    ).fetchall()
    print('\n=== ОЦЕНКИ ===')
    for r in rt:
        print(dict(r))

# ── 6. Long-анонс 07.06 ───────────────────────────────────────
print('\n=== LONG-АНОНС (последние long) ===')
la = c.execute(
    "SELECT workout_date, post_id, analyzed_json FROM workout_analysis "
    "WHERE workout_type='long' ORDER BY workout_date DESC LIMIT 3"
).fetchall()
for row in la:
    print(f"\n--- {row['workout_date']} (post {row['post_id']}) ---")
    try:
        d = json.loads(row['analyzed_json'])
        for g in (d.get('groups') or []):
            print(f"  Группа {g.get('number')}: work={g.get('work')!r} "
                  f"pace_start={g.get('pace_start')} pace_end={g.get('pace_end')} "
                  f"prog={g.get('progression') or g.get('has_progression')}")
        print('  has_progression:', d.get('has_progression'),
              '| even_pace_available:', d.get('even_pace_available'))
    except Exception as e:
        print('  parse error:', e)
