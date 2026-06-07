"""Показать сегодняшний слепок на утро по всем юзерам (читает unified_cache, без записи)."""
import sys, sqlite3
sys.path.insert(0, '/opt/running-bot/src')

c = sqlite3.connect('running_bot.db')
c.row_factory = sqlite3.Row

rows = c.execute(
    "SELECT user_id, morning_date, morning_tr, morning_bb, morning_hrv, "
    "morning_rhr, morning_sleep_h, morning_wake_at, morning_snapshot_at "
    "FROM unified_cache WHERE morning_caught=1 ORDER BY user_id"
).fetchall()

# имена
names = {r[0]: (r[1] or r[2] or ('uid_%s' % r[0]))
         for r in c.execute("SELECT id, username, name FROM users")}

print('=== Слепок на утро (%d юзеров) ===' % len(rows))
for r in rows:
    wake = (r['morning_wake_at'] or '—')
    wake_hm = wake[11:16] if len(wake) >= 16 else wake
    snap = (r['morning_snapshot_at'] or '')[11:16]
    print('uid=%3s %-14s | TR=%-4s BB=%-4s HRV=%-5s RHR=%-3s сон=%-5s пробужд=%-5s (снято %s UTC)' % (
        r['user_id'], str(names.get(r['user_id'], ''))[:14],
        r['morning_tr'], r['morning_bb'], r['morning_hrv'], r['morning_rhr'],
        r['morning_sleep_h'], wake_hm, snap))
