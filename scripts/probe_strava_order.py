"""В каком порядке Strava отдаёт активности в get_recent_activities.

Печатает первые и последние 5 активностей списка с датами — видно, ascending
или descending. Заодно показывает, что станет runs[0] в _strava_candidate.

Запуск (на сервере): venv/bin/python3 scripts/probe_strava_order.py [db_user_id]
"""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import strava  # noqa: E402

UID = int(sys.argv[1]) if len(sys.argv) > 1 else 2


async def main():
    token = await strava.ensure_valid_token(UID)
    if not token:
        print("нет токена")
        return
    acts = await strava.get_recent_activities(token, days=30)
    print(f"всего активностей за 30 дней: {len(acts)}")
    print("\nпервые 5 в списке:")
    for a in acts[:5]:
        print(f"  {a.get('start_date_local')}  {a.get('type')}  {a.get('name')}")
    print("\nпоследние 5 в списке:")
    for a in acts[-5:]:
        print(f"  {a.get('start_date_local')}  {a.get('type')}  {a.get('name')}")
    runs = [a for a in acts
            if a.get("type") == "Run" and re.search(r"DD[-_]", str(a.get("name") or ""))]
    print(f"\nDD-пробежек: {len(runs)}")
    if runs:
        print(f"runs[0] (его и берёт /report без селектора): "
              f"{runs[0].get('start_date_local')}  {runs[0].get('name')}")
        print(f"самая свежая по дате:                        "
              f"{max(runs, key=lambda a: a.get('start_date_local') or '').get('start_date_local')}  "
              f"{max(runs, key=lambda a: a.get('start_date_local') or '').get('name')}")


asyncio.run(main())
