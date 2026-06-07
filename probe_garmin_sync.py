"""Garmin raw uid=2: ищем все поля времени синка часов с приложением."""
import sys, json
sys.path.insert(0, '/opt/running-bot/src')
from database import get_raw_service_data

row = get_raw_service_data(2, "garmin")
print("fetched_at (наш забор):", row["fetched_at"] if row else None)
g = json.loads(row["raw_json"]) if row else {}

print("\nКлючи raw:", list(g.keys()))

us = g.get("user_summary") or {}
print("\n--- user_summary: поля с sync/timestamp/time ---")
for k, v in us.items():
    kl = k.lower()
    if any(s in kl for s in ("sync", "timestamp", "time", "gmt", "date")):
        print(f"  {k} = {v}")

# device last used / sync
dev = g.get("device_last_used") or g.get("devices") or g.get("device")
print("\n--- device info ---")
print("  device_last_used:", json.dumps(dev, ensure_ascii=False)[:500] if dev else None)

# bb с синком
bb = g.get("body_battery") or {}
if isinstance(bb, dict):
    print("\n--- body_battery: поля с sync/time ---")
    for k, v in bb.items():
        kl = k.lower()
        if any(s in kl for s in ("sync", "timestamp", "time", "gmt", "date")):
            print(f"  {k} = {v}")
