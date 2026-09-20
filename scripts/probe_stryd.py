"""probe_stryd.py — разведка (20.09.2026): видно ли по активности Garmin, что был подключён Stryd.

Ничего не меняет ни в базе, ни в Garmin. Для каждой беговой дорожки (treadmill_running)
с 2026-08-01 среди последних 200 активностей печатает одну строку с признаками:
  шаг  — среди датчиков есть шагомер (ANT+ STRIDE_SPEED_DISTANCE);
  мощн — внешний датчик мощности не от Garmin (ANT+ BIKE_POWER, manufacturer != GARMIN);
  ciq  — есть данные приложений Connect IQ (appID, первые 8 знаков).
Ожидание (Антон): Stryd с 18.08.2026 — до этой даты признаков нет, после есть.

Запуск на сервере:  cd /opt/running-bot && venv/bin/python scripts/probe_stryd.py 2
(2 — db_user_id Антона). bot.py не импортирует.
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")
import garmin  # noqa: E402

SINCE = "2026-08-01"


def flags(full):
    """(шагомер, внешняя мощность, набор appID Connect IQ) по полному ответу активности."""
    sensors = ((full or {}).get("metadataDTO") or {}).get("sensors") or []
    step = any(s.get("antplusDeviceType") == "STRIDE_SPEED_DISTANCE" for s in sensors)
    power = any(s.get("antplusDeviceType") == "BIKE_POWER" and s.get("manufacturer") != "GARMIN"
                for s in sensors)
    ciq = sorted({str(m.get("appID"))[:8] for m in ((full or {}).get("connectIQMeasurements") or [])})
    return step, power, ciq


async def main(uid):
    client = await garmin._client(uid)
    if not client:
        print("Нет входа в Garmin для этого пользователя")
        return
    acts = await asyncio.to_thread(client.get_activities, 0, 200)
    tread = [a for a in acts
             if "treadmill" in str((a.get("activityType") or {}).get("typeKey"))
             and str(a.get("startTimeLocal")) >= SINCE]
    print(f"Дорожки с {SINCE} (новые сверху): дата | шаг | мощн | ciq | имя")
    for a in tread:
        try:
            full = await asyncio.to_thread(client.get_activity, a.get("activityId"))
        except Exception as e:  # noqa: BLE001
            print(f"  {a.get('startTimeLocal')}  ошибка: {type(e).__name__}")
            continue
        step, power, ciq = flags(full)
        yn = lambda v: "да " if v else "нет"  # noqa: E731
        print(f"  {str(a.get('startTimeLocal'))[:16]} | {yn(step)} | {yn(power)} | "
              f"{','.join(ciq) or '—':<9} | {a.get('activityName')}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2))
