import asyncio, time
from dotenv import load_dotenv
load_dotenv('../.env')
from database import get_token, save_token
from strava import refresh_access_token, get_training_load, get_last_race

async def test():
    t = get_token(2, 'strava')
    # Обновляем токен
    new = await refresh_access_token(t['refresh_token'])
    if 'access_token' in new:
        save_token(2, 'strava', new['access_token'], new['refresh_token'], str(new['expires_at']))
        token = new['access_token']
        print('Токен обновлён')
    else:
        token = t['access_token']

    print("=== НАГРУЗКА ===")
    load = await get_training_load(token)
    print(load['summary'])

    print("\n=== СОРЕВНОВАНИЯ ===")
    race = await get_last_race(token)
    print(race)

asyncio.run(test())