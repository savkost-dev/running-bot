import sys
sys.path.insert(0, '/opt/running-bot/src')
from database import get_morning_caught
for uid in (2,):
    print(uid, get_morning_caught(uid))
