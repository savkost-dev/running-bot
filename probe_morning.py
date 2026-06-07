"""Свежий синк Garmin сейчас + что в ответе про УТРО.
1) training_readiness — все score+timestamp (помнит ли утренние).
2) user_summary.bodyBatteryAtWakeTime — заряд на пробуждении (фиксирован?).
3) sleep_data.sleepBodyBattery — BB по фазам сна (есть ли утренний).
"""
import asyncio, sys, json
sys.path.insert(0, '/opt/running-bot/src')
import garmin, database as db

async def main():
    await garmin.fetch_raw(2)
    raw = json.loads(db.get_raw_service_data(2, 'garmin')['raw_json'])

    print('=== training_readiness (score @ timestamp) ===')
    tr = raw.get('training_readiness') or []
    for it in (tr if isinstance(tr, list) else [tr]):
        if isinstance(it, dict):
            print('  score=%-4s level=%-9s @ %s' % (
                it.get('score'), it.get('level'), it.get('timestampLocal')))

    us = raw.get('user_summary') or {}
    print('\n=== user_summary ===')
    print('  bodyBatteryAtWakeTime =', us.get('bodyBatteryAtWakeTime'))
    print('  bodyBatteryMostRecentValue =', us.get('bodyBatteryMostRecentValue'))

    sd = raw.get('sleep_data') or {}
    sbb = sd.get('sleepBodyBattery')
    print('\n=== sleep_data.sleepBodyBattery тип=%s ===' % type(sbb).__name__)
    if isinstance(sbb, list) and sbb:
        print('  первая:', sbb[0])
        print('  последняя:', sbb[-1])

asyncio.run(main())
