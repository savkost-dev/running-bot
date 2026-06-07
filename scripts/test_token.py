from dotenv import load_dotenv
load_dotenv('../.env')
from database import get_or_create_user, get_token

# Твой реальный Telegram ID
telegram_id = 273726778
db_user_id = get_or_create_user(telegram_id, 'Anton')
print(f'db_user_id: {db_user_id}')

token = get_token(db_user_id, 'strava')
print(f'strava token: {token}')