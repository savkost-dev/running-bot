"""uid 7 (polar): что есть в сыром nightly_recharge и sleep — какие поля можно класть в снимок."""
import sys, json
sys.path.insert(0, '/opt/running-bot/src')
import database as db

raw = json.loads(db.get_raw_service_data(7, 'polar')['raw_json'])
print('ключи polar:', list(raw.keys()))

nr = raw.get('nightly_recharge')
items = nr if isinstance(nr, list) else ((nr or {}).get('recharges') or (nr or {}).get('items') or [])
items = [x for x in items if isinstance(x, dict)]
print('\nnightly_recharge элементов:', len(items))
if items:
    print('последняя запись, ключи:', list(items[-1].keys()))
    print('последняя запись:', json.dumps(items[-1], ensure_ascii=False)[:600])

sl = raw.get('sleep')
nights = sl if isinstance(sl, list) else ((sl or {}).get('nights') or (sl or {}).get('items') or [])
nights = [x for x in nights if isinstance(x, dict)]
print('\nsleep ночей:', len(nights))
if nights:
    print('последняя ночь, ключи:', list(nights[-1].keys()))
