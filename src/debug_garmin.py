import sys, json
sys.path.insert(0, "/opt/running-bot/src")
from database import get_all_users, get_or_create_user, get_token
from garminconnect import Garmin

users = get_all_users()
for tg_id, name, _ in users:
    db_id = get_or_create_user(tg_id, name)
    td = get_token(db_id, "garmin")
    if td:
        client = Garmin()
        client.login(tokenstore=td["access_token"])
        print("--- get_lactate_threshold ---")
        data = client.get_lactate_threshold()
        print(json.dumps(data, indent=2, default=str)[:2000])
        break
