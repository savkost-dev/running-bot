import asyncio, time
from dotenv import load_dotenv
load_dotenv('../.env')
from database import get_token, save_token
from whoop import refresh_access_token, get_full_recovery_data

async def test():
    t = get_token(2, 'whoop')
    new = await refresh_access_token(t['refresh_token'])
    access_token = new['access_token']
    save_token(2, 'whoop', access_token, new.get('refresh_token'),
               str(int(time.time()) + new.get('expires_in', 3600)))
    
    data = await get_full_recovery_data(access_token)
    print(data)

asyncio.run(test())