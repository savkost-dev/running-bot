"""\u0420\u0430\u0437\u0432\u0435\u0434\u043a\u0430 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432 Garmin \u0443 \u0432\u0441\u0435\u0445 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d\u043d\u044b\u0445 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0435\u0439 (19.08.2026).

\u0422\u043e\u043b\u044c\u043a\u043e \u0427\u0422\u0415\u041d\u0418\u0415: \u043d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u043f\u0438\u0448\u0435\u0442 \u0432 \u0431\u0430\u0437\u0443 \u0438 \u043d\u0435 \u0448\u043b\u0451\u0442 \u0432 Garmin.
\u0426\u0435\u043b\u044c \u2014 \u043f\u043e\u043d\u044f\u0442\u044c, \u043f\u043e \u043a\u0430\u043a\u0438\u043c \u043f\u043e\u043b\u044f\u043c \u0444\u0438\u043b\u044c\u0442\u0440\u043e\u0432\u0430\u0442\u044c \u0446\u0435\u043b\u0435\u0432\u043e\u0435 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u043e \u0434\u043b\u044f \u043f\u0443\u0448\u0430 \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0438:
\u043c\u043e\u0434\u0435\u043b\u044c, \u0434\u0430\u0442\u0430 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0433\u043e \u0441\u0438\u043d\u043a\u0430, \u043f\u0440\u0438\u0437\u043d\u0430\u043a\u0438 \u043f\u043e\u0434\u0434\u0435\u0440\u0436\u043a\u0438 \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043e\u043a.

\u0417\u0430\u043f\u0443\u0441\u043a \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0435:
    cd /opt/running-bot && venv/bin/python3 scripts/probe_garmin_devices.py
"""
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import get_connection  # noqa: E402
import garmin  # noqa: E402

# \u041f\u043e\u043b\u044f, \u043a\u043e\u0442\u043e\u0440\u044b\u0435 \u0438\u043d\u0442\u0435\u0440\u0435\u0441\u043d\u044b \u0434\u043b\u044f \u0444\u0438\u043b\u044c\u0442\u0440\u0430\u0446\u0438\u0438 (\u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u043c, \u0435\u0441\u043b\u0438 \u0435\u0441\u0442\u044c)
FLAG_KEYS = [
    "workoutSupported", "structuredWorkoutSupported", "runningWorkoutSupported",
    "primaryTrainingDevice", "primaryActivityTrackerIndicator", "wifiSetup",
    "deviceCategories", "productDisplayName", "partNumber",
]


def _fmt_sync(val):
    if not val:
        return "\u2014"
    s = str(val)[:19].replace("T", " ")
    try:
        days = (datetime.utcnow() - datetime.fromisoformat(str(val)[:19])).days
        return f"{s} ({days} \u0434\u043d. \u043d\u0430\u0437\u0430\u0434)"
    except Exception:
        return s


async def main():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT u.id, COALESCE(u.username, u.name, u.telegram_id) "
            "FROM users u JOIN user_profile p ON p.user_id = u.id "
            "WHERE p.garmin_email IS NOT NULL AND p.garmin_email != '' "
            "ORDER BY u.id"
        ).fetchall()

    print(f"\u0413\u0430\u0440\u043c\u0438\u043d-\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0435\u0439 \u0432 \u0431\u0430\u0437\u0435: {len(rows)}\n")
    total_devices = 0
    for db_user_id, who in rows:
        client = await garmin._client(db_user_id)
        if not client:
            print(f"uid {db_user_id} ({who}): \u043a\u043b\u0438\u0435\u043d\u0442 \u043d\u0435 \u0441\u043e\u0437\u0434\u0430\u043d (\u043a\u0440\u0435\u0434\u044b/\u0431\u043b\u043e\u043a)")
            continue
        try:
            def _get():
                fn = getattr(client, "connectapi", None) or client.garth.connectapi
                return fn("/device-service/deviceregistration/devices") or []
            devices = await asyncio.to_thread(_get)
        except Exception as e:
            print(f"uid {db_user_id} ({who}): \u043e\u0448\u0438\u0431\u043a\u0430 {type(e).__name__}: {str(e)[:60]}")
            continue

        total_devices += len(devices)
        print(f"uid {db_user_id} ({who}): \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432 {len(devices)}")
        for d in devices:
            name = (d.get("displayName") or d.get("productDisplayName")
                    or d.get("deviceTypePk") or "?")
            flags = {k: d.get(k) for k in FLAG_KEYS if d.get(k) is not None}
            print(f"   \u2022 {name} | id={d.get('deviceId')} | "
                  f"sync={_fmt_sync(d.get('lastSyncTime') or d.get('lastUsedDeviceUploadTime'))}")
            if flags:
                print(f"     {flags}")
        # \u0435\u0434\u0438\u043d\u043e\u0436\u0434\u044b \u2014 \u043f\u043e\u043b\u043d\u044b\u0439 \u043d\u0430\u0431\u043e\u0440 \u043a\u043b\u044e\u0447\u0435\u0439 \u043f\u0435\u0440\u0432\u043e\u0433\u043e \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0430 (\u0447\u0442\u043e \u0432\u043e\u043e\u0431\u0449\u0435 \u0435\u0441\u0442\u044c \u0432 \u043e\u0442\u0432\u0435\u0442\u0435)
        if devices and db_user_id == rows[0][0]:
            print(f"   \u0432\u0441\u0435 \u043f\u043e\u043b\u044f \u043e\u0442\u0432\u0435\u0442\u0430: {sorted(devices[0].keys())}")
        print()

    print(f"\u0418\u0442\u043e\u0433\u043e \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432: {total_devices}")


if __name__ == "__main__":
    asyncio.run(main())
