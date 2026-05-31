import os
import asyncio
import logging
from datetime import datetime, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TimedOut, NetworkError, Forbidden
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from dotenv import load_dotenv

load_dotenv()

from version import VERSION, BUILD_DATE
from weather import get_weather_for_workout, format_weather_for_message, format_weather_for_prompt
from database import (
    init_db, get_or_create_user, get_token, save_token,
    get_all_users, get_active_users, get_all_users_with_status, get_inactive_users,
    save_athlete_cache, get_athlete_cache,
    save_user_profile, get_user_profile,
    get_preferences, set_preference,
    save_last_recommendation, get_last_recommendation,
    get_workout_notification, save_workout_notification, get_last_workout_notification,
    get_users_for_notification,
    get_garmin_recovery_cache, save_garmin_recovery_cache,
    user_exists, log_activity, get_bot_stats, count_users_with_service,
    delete_token,
    get_all_users_with_details, get_users_with_service_full, get_users_with_profile_full,
    save_feedback, save_rating, get_recent_ratings, get_recent_feedbacks,
    save_workout_analysis, get_workout_analysis, get_latest_workout_analysis,
    get_preprocess_mode, set_preprocess_mode,
)
from strava import (
    get_auth_url, get_recent_runs, analyze_fitness,
    ensure_valid_token, get_full_athlete_data
)
from telegram_reader import (
    find_next_workout, find_next_long_run, format_workout_message,
    get_latest_workout_post_id, get_latest_long_run_post_id, get_extra_groups_for_post,
    get_latest_workout_post_full,
)
import claude_advisor
from claude_advisor import (
    build_evening_prompt, build_morning_prompt, build_long_run_prompt,
    ask_groq, format_evening_message, format_morning_message, format_long_run_message,
    analyze_workout,
)
import zones

ADMIN_TELEGRAM_IDS = {273726778}
ADMIN_ID = 273726778


def _mark_user_inactive(telegram_id: int) -> None:
    """РџРѕРјРµС‡Р°РµС‚ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ РєР°Рє РЅРµР°РєС‚РёРІРЅРѕРіРѕ (Р·Р°Р±Р»РѕРєРёСЂРѕРІР°Р» Р±РѕС‚Р°)."""
    try:
        db_user_id = get_or_create_user(telegram_id, "")
        set_preference(db_user_id, "is_active", 0)
        set_preference(db_user_id, "deactivated_at", datetime.now().isoformat())
    except Exception as e:
        logger.error(f"Failed to mark user {telegram_id} inactive: {e}")


def _mark_user_active_if_needed(telegram_id: int, name: str = "", username: str = None) -> int:
    """Р’РѕСЃСЃС‚Р°РЅР°РІР»РёРІР°РµС‚ is_active=1 РїСЂРё РІС…РѕРґСЏС‰РµРј СЃРѕРѕР±С‰РµРЅРёРё. Р’РѕР·РІСЂР°С‰Р°РµС‚ db_user_id."""
    db_user_id = get_or_create_user(telegram_id, name, username)
    prefs = get_preferences(db_user_id)
    if prefs and not prefs.get("is_active", True):
        set_preference(db_user_id, "is_active", 1)
        set_preference(db_user_id, "deactivated_at", None)
        logger.info(f"РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ {telegram_id} СЃРЅРѕРІР° Р°РєС‚РёРІРµРЅ")
    return db_user_id


async def _notify_admin(bot, text: str) -> None:
    """РћС‚РїСЂР°РІР»СЏРµС‚ СѓРІРµРґРѕРјР»РµРЅРёРµ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂСѓ. РўРёС…Рѕ РёРіРЅРѕСЂРёСЂСѓРµС‚ РѕС€РёР±РєРё."""
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as e:
        logger.warning(f"Admin notify failed: {e}")


last_workout: dict | None = None
last_long_run: dict | None = None

# In-memory cache: telegram_id в†’ FIT generation params (set after recommendation)
_fit_data: dict[int, dict] = {}
# In-memory cache: telegram_id в†’ rating context (workout_date, ai_mode)
_rating_data: dict[int, dict] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


# в”Ђв”Ђ РљР­РЁ РђРўР›Р•РўРђ в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

async def refresh_athlete_cache(db_user_id: int, access_token: str, notify_msg=None) -> dict | None:
    """
    РћР±РЅРѕРІР»СЏРµС‚ РєСЌС€ РґР°РЅРЅС‹С… Р°С‚Р»РµС‚Р° (CTL/ATL/TSB, РїСЂРѕРіРЅРѕР·С‹, СЃРѕСЂРµРІРЅРѕРІР°РЅРёСЏ).
    Р—Р°РЅРёРјР°РµС‚ 30-60 СЃРµРє вЂ” РІС‹Р·С‹РІР°С‚СЊ С‚РѕР»СЊРєРѕ РїСЂРё РїРѕРґРєР»СЋС‡РµРЅРёРё РёР»Рё РїРѕ Р·Р°РїСЂРѕСЃСѓ.
    """
    try:
        if notify_msg:
            await notify_msg.edit_text(
                "вЏі Р—Р°РіСЂСѓР¶Р°СЋ РґР°РЅРЅС‹Рµ РёР· Strava...\n"
                "Р­С‚Рѕ Р·Р°Р№РјС‘С‚ РѕРєРѕР»Рѕ РјРёРЅСѓС‚С‹ (С‚РѕР»СЊРєРѕ РїРµСЂРІС‹Р№ СЂР°Р·)"
            )

        athlete_data = await get_full_athlete_data(access_token)

        save_athlete_cache(
            db_user_id,
            athlete_data["training_load"],
            athlete_data["predictions"],
            athlete_data["last_race"]
        )

        logger.info(f"РљСЌС€ Р°С‚Р»РµС‚Р° РѕР±РЅРѕРІР»С‘РЅ РґР»СЏ user_id={db_user_id}")
        return athlete_data

    except Exception as e:
        logger.error(f"РћС€РёР±РєР° РѕР±РЅРѕРІР»РµРЅРёСЏ РєСЌС€Р° РґР»СЏ user_id={db_user_id}: {e}")
        return None


async def get_fitness_data(db_user_id: int, access_token: str) -> dict | None:
    """
    РџРѕР»СѓС‡Р°РµС‚ РґР°РЅРЅС‹Рµ Р°С‚Р»РµС‚Р°:
    - Р‘С‹СЃС‚СЂС‹Рµ (РІСЃРµРіРґР° СЃРІРµР¶РёРµ): РїСЂРѕР±РµР¶РєРё Р·Р° 14 РґРЅРµР№ + РѕСЃС‚СЂР°СЏ РЅР°РіСЂСѓР·РєР° Р·Р° 48 С‡
    - РњРµРґР»РµРЅРЅС‹Рµ (РёР· РєСЌС€Р°): CTL/ATL/TSB, РїСЂРѕРіРЅРѕР·С‹ Р РёРµРіРµР»СЏ, РїРѕСЃР»РµРґРЅРµРµ СЃРѕСЂРµРІРЅРѕРІР°РЅРёРµ
    """
    from strava import get_recent_runs, analyze_fitness, get_recent_48h_load

    # в”Ђв”Ђ Р‘С‹СЃС‚СЂС‹Рµ РґР°РЅРЅС‹Рµ (2 Р·Р°РїСЂРѕСЃР° Рє Strava API) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    try:
        runs, load_48h = await asyncio.gather(
            get_recent_runs(access_token, days=14),
            get_recent_48h_load(access_token),
        )
        fitness = analyze_fitness(runs)
        fitness["load_48h"] = load_48h
    except Exception as e:
        logger.error(f"РћС€РёР±РєР° РїРѕР»СѓС‡РµРЅРёСЏ РїСЂРѕР±РµР¶РµРє: {e}")
        fitness = {"summary": "РќРµС‚ РґР°РЅРЅС‹С…", "total_km": 0, "run_count": 0,
                   "avg_pace": "вЂ”", "avg_hr": None, "fatigue_level": "unknown",
                   "load_48h": None}

    # в”Ђв”Ђ РњРµРґР»РµРЅРЅС‹Рµ РґР°РЅРЅС‹Рµ (РёР· РєСЌС€Р°) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    cache = get_athlete_cache(db_user_id)
    if cache:
        fitness["training_load"] = cache["training_load"]
        fitness["predictions"]   = cache["predictions"]
        fitness["last_race"]     = cache["last_race"]
    else:
        logger.info(f"РљСЌС€ РѕС‚СЃСѓС‚СЃС‚РІСѓРµС‚ РґР»СЏ user_id={db_user_id}, РѕР±РЅРѕРІР»СЏСЋ...")
        athlete_data = await refresh_athlete_cache(db_user_id, access_token)
        if athlete_data:
            fitness["training_load"] = athlete_data["training_load"]
            fitness["predictions"]   = athlete_data["predictions"]
            fitness["last_race"]     = athlete_data["last_race"]

    return fitness


async def get_garmin_fitness_data(db_user_id: int) -> dict | None:
    """
    РџРѕР»СѓС‡Р°РµС‚ РґР°РЅРЅС‹Рµ Р°С‚Р»РµС‚Р° РёР· Garmin Connect вЂ” Р°РЅР°Р»РѕРі get_fitness_data() РґР»СЏ Strava.
    Р’РѕР·РІСЂР°С‰Р°РµС‚ dict, СЃРѕРІРјРµСЃС‚РёРјС‹Р№ СЃРѕ СЃС‚СЂСѓРєС‚СѓСЂРѕР№ fitness РґР»СЏ РїСЂРѕРјС‚Р°.
    """
    import garmin as _garmin
    if not get_token(db_user_id, "garmin"):
        return None

    try:
        results = await asyncio.gather(
            _garmin.get_training_load(db_user_id),
            _garmin.get_activities_48h(db_user_id),
            _garmin.get_last_race(db_user_id),
            _garmin.get_best_efforts(db_user_id),
            return_exceptions=True,
        )
    except Exception as e:
        logger.error(f"Garmin fitness data error for {db_user_id}: {e}")
        return None

    training_load, activities_48h, last_race, best_efforts = results

    fitness = {
        "source": "garmin",
        "summary": "",
        "total_km": 0,
        "run_count": 0,
        "avg_pace": "вЂ”",
        "avg_hr": None,
        "fatigue_level": "unknown",
    }

    if not isinstance(training_load, Exception) and training_load:
        fitness["training_load"] = training_load
        tsb = training_load.get("tsb")
        if tsb is not None:
            fitness["fatigue_level"] = "fresh" if tsb > 5 else ("tired" if tsb < -15 else "normal")

    if not isinstance(activities_48h, Exception) and activities_48h:
        fitness["load_48h"] = activities_48h
        fitness["total_km"] = activities_48h.get("km_48h", 0)
        fitness["run_count"] = activities_48h.get("sessions_48h", 0)

    if not isinstance(last_race, Exception) and last_race:
        fitness["last_race"] = last_race

    if not isinstance(best_efforts, Exception) and best_efforts:
        fitness["predictions"] = best_efforts

    return fitness


async def get_coros_fitness_data(db_user_id: int) -> dict | None:
    """
    РџРѕР»СѓС‡Р°РµС‚ РґР°РЅРЅС‹Рµ Р°С‚Р»РµС‚Р° РёР· COROS вЂ” Р°РЅР°Р»РѕРі get_garmin_fitness_data().
    Р’РѕР·РІСЂР°С‰Р°РµС‚ dict, СЃРѕРІРјРµСЃС‚РёРјС‹Р№ СЃРѕ СЃС‚СЂСѓРєС‚СѓСЂРѕР№ fitness РґР»СЏ РїСЂРѕРјС‚Р°.
    """
    import coros as _coros
    if not get_token(db_user_id, "coros"):
        return None
    try:
        return await _coros.get_full_data(db_user_id)
    except Exception as e:
        logger.error(f"COROS fitness data error for {db_user_id}: {e}")
        return None


async def get_polar_fitness_data(db_user_id: int) -> dict | None:
    """
    РџРѕР»СѓС‡Р°РµС‚ РґР°РЅРЅС‹Рµ Р°С‚Р»РµС‚Р° РёР· Polar вЂ” Р°РЅР°Р»РѕРі get_coros_fitness_data().
    Р’РѕР·РІСЂР°С‰Р°РµС‚ dict, СЃРѕРІРјРµСЃС‚РёРјС‹Р№ СЃРѕ СЃС‚СЂСѓРєС‚СѓСЂРѕР№ fitness РґР»СЏ РїСЂРѕРјС‚Р°.
    """
    import polar as _polar
    if not get_token(db_user_id, "polar"):
        return None
    try:
        return await _polar.get_full_data(db_user_id)
    except Exception as e:
        logger.error(f"Polar fitness data error for {db_user_id}: {e}")
        return None


# в”Ђв”Ђ РќРђР’РР“РђР¦РРЇ в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

def get_main_keyboard(from_recommendation: bool = False) -> InlineKeyboardMarkup:
    """РљСЂР°С‚РєРѕРµ РјРµРЅСЋ РїРѕРґ РєР°Р¶РґС‹Рј РѕС‚РІРµС‚РѕРј.
    from_recommendation=True в†’ РіР»Р°РІРЅРѕРµ РјРµРЅСЋ РѕС‚РєСЂС‹РІР°РµС‚СЃСЏ РЅРѕРІС‹Рј СЃРѕРѕР±С‰РµРЅРёРµРј (/start-РїРѕРІРµРґРµРЅРёРµ).
    from_recommendation=False в†’ СЂРµРґР°РєС‚РёСЂСѓРµС‚ С‚РµРєСѓС‰РµРµ СЃРѕРѕР±С‰РµРЅРёРµ (РЅР°РІРёРіР°С†РёРѕРЅРЅС‹Рµ СЌРєСЂР°РЅС‹).
    """
    home_data = "main_menu_new" if from_recommendation else "main_menu"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("рџ“‹ РўСЂРµРЅРёСЂРѕРІРєР°", callback_data="get_workout"),
         InlineKeyboardButton("рџ•ђ Long Run",   callback_data="get_long_run")],
        [InlineKeyboardButton("рџЏ  Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ", callback_data=home_data)],
    ])


def _merge_keyboards(*keyboards) -> InlineKeyboardMarkup:
    """РћР±СЉРµРґРёРЅСЏРµС‚ РЅРµСЃРєРѕР»СЊРєРѕ InlineKeyboardMarkup РІ РѕРґРёРЅ."""
    rows = []
    for kb in keyboards:
        if kb:
            rows.extend(kb.inline_keyboard)
    return InlineKeyboardMarkup(rows)


def _build_screen1_keyboard() -> InlineKeyboardMarkup:
    """Р­РєСЂР°РЅ 1 /start вЂ” РѕСЃРЅРѕРІРЅС‹Рµ РґРµР№СЃС‚РІРёСЏ."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("рџ“‹ РўСЂРµРЅРёСЂРѕРІРєР°", callback_data="get_workout"),
         InlineKeyboardButton("рџ•ђ Long Run",   callback_data="get_long_run")],
        [InlineKeyboardButton("вЂпёЏ РЈС‚СЂРѕ",       callback_data="get_morning"),
         InlineKeyboardButton("рџ§  Р РµР¶РёРј AI",   callback_data="ai_mode")],
        [InlineKeyboardButton("рџ’¬ РћР±СЂР°С‚РЅР°СЏ СЃРІСЏР·СЊ", callback_data="feedback_show"),
         InlineKeyboardButton("вќ“ РЎРїСЂР°РІРєР°",        callback_data="help")],
        [InlineKeyboardButton("вљ™пёЏ РќР°СЃС‚СЂРѕР№РєРё в†’",   callback_data="show_settings")],
    ])


def _build_screen1_onboarding_keyboard() -> InlineKeyboardMarkup:
    """Р­РєСЂР°РЅ 1 вЂ” РѕРЅР±РѕСЂРґРёРЅРі РЅРѕРІРѕРіРѕ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ (РЅРµС‚ РїСЂРѕС„РёР»СЏ РёР»Рё С‚СЂРµРєРµСЂР°)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("рџ‘¤ 1. Р—Р°РїРѕР»РЅРёС‚СЊ РїСЂРѕС„РёР»СЊ",  callback_data="my_profile")],
        [InlineKeyboardButton("рџ”— 2. РџРѕРґРєР»СЋС‡РёС‚СЊ С‚СЂРµРєРµСЂ",  callback_data="show_services")],
        [InlineKeyboardButton("рџ“‹ РўСЂРµРЅРёСЂРѕРІРєР°", callback_data="get_workout"),
         InlineKeyboardButton("рџ•ђ Long Run",   callback_data="get_long_run")],
    ])


def _build_screen2_keyboard() -> InlineKeyboardMarkup:
    """Р­РєСЂР°РЅ 2 вЂ” РЅР°СЃС‚СЂРѕР№РєРё."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("рџ‘¤ РџСЂРѕС„РёР»СЊ",        callback_data="my_profile"),
         InlineKeyboardButton("рџ”” РЈРІРµРґРѕРјР»РµРЅРёСЏ",    callback_data="notifications")],
        [InlineKeyboardButton("рџ”„ РћР±РЅРѕРІРёС‚СЊ РґР°РЅРЅС‹Рµ", callback_data="refresh_cache"),
         InlineKeyboardButton("рџ”— РЎРµСЂРІРёСЃС‹ в†’",       callback_data="show_services")],
        [InlineKeyboardButton("в†ђ РќР°Р·Р°Рґ",            callback_data="main_menu")],
    ])


def _settings_nav() -> list:
    """РЎС‚СЂРѕРєР° РЅР°РІРёРіР°С†РёРё: в†ђ РќР°СЃС‚СЂРѕР№РєРё + рџЏ  Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ."""
    return [
        InlineKeyboardButton("в†ђ РќР°СЃС‚СЂРѕР№РєРё", callback_data="settings_menu"),
        InlineKeyboardButton("рџЏ  Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ", callback_data="main_menu"),
    ]


# РњРµС‚Р°РґР°РЅРЅС‹Рµ СЃРµСЂРІРёСЃРѕРІ: (РєР»СЋС‡, СЌРјРѕРґР·Рё, РЅР°Р·РІР°РЅРёРµ, connect_callback, disconnect_done_msg)
_SERVICES = [
    ("strava", "рџџ ", "Strava",  "connect_strava",      "Strava РѕС‚РєР»СЋС‡РµРЅР°"),
    ("whoop",  "вљЄ", "Whoop",   "connect_whoop_btn",   "Whoop РѕС‚РєР»СЋС‡С‘РЅ"),
    ("garmin", "рџ”µ", "Garmin",  "connect_garmin_btn",  "Garmin РѕС‚РєР»СЋС‡С‘РЅ"),
    ("coros",  "рџ”ґ", "COROS",   "connect_coros_btn",   "COROS РѕС‚РєР»СЋС‡С‘РЅ"),
    ("polar",  "вќ„пёЏ", "Polar",   "connect_polar_btn",   "Polar РѕС‚РєР»СЋС‡С‘РЅ"),
]


def _svc_name(svc: str) -> str:
    """Р’РѕР·РІСЂР°С‰Р°РµС‚ РѕС‚РѕР±СЂР°Р¶Р°РµРјРѕРµ РёРјСЏ СЃРµСЂРІРёСЃР° РїРѕ РєР»СЋС‡Сѓ."""
    return next((name for s, _, name, _, _ in _SERVICES if s == svc), svc)


def _svc_done_msg(svc: str) -> str:
    """Р’РѕР·РІСЂР°С‰Р°РµС‚ СЃРѕРѕР±С‰РµРЅРёРµ РїРѕСЃР»Рµ РѕС‚РєР»СЋС‡РµРЅРёСЏ СЃРµСЂРІРёСЃР°."""
    return next((msg for s, _, _, _, msg in _SERVICES if s == svc), f"{svc} РѕС‚РєР»СЋС‡С‘РЅ")


def _build_screen3_keyboard(db_user_id: int) -> InlineKeyboardMarkup:
    """Р­РєСЂР°РЅ 3 вЂ” РїРѕРґРєР»СЋС‡РµРЅРёРµ/РѕС‚РєР»СЋС‡РµРЅРёРµ СЃРµСЂРІРёСЃРѕРІ.

    РџРѕРґРєР»СЋС‡С‘РЅ:    [рџџ  Strava вњ…]  [вќЊ РћС‚РєР»СЋС‡РёС‚СЊ]
    РќРµ РїРѕРґРєР»СЋС‡С‘РЅ: [рџџ  Strava вќЊ  РџРѕРґРєР»СЋС‡РёС‚СЊ]
    """
    rows = []
    for svc, emoji, name, connect_cb, _ in _SERVICES:
        if get_token(db_user_id, svc):
            rows.append([
                InlineKeyboardButton(f"{emoji} {name} вњ…", callback_data="svc_noop"),
                InlineKeyboardButton("вќЊ РћС‚РєР»СЋС‡РёС‚СЊ",        callback_data=f"disc_ask_{svc}"),
            ])
        else:
            if svc == "strava":
                label = f"{emoji} {name} вќЊ  (РЅР° РїСЂРѕРІРµСЂРєРµ)"
            else:
                label = f"{emoji} {name} вќЊ  РџРѕРґРєР»СЋС‡РёС‚СЊ"
            rows.append([InlineKeyboardButton(label, callback_data=connect_cb)])
    rows.append(_settings_nav())
    return InlineKeyboardMarkup(rows)


def _build_main_menu_content(user, db_user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """РЎС‚СЂРѕРёС‚ (text, keyboard) РґР»СЏ РіР»Р°РІРЅРѕРіРѕ РјРµРЅСЋ вЂ” РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ Рё РїСЂРё edit, Рё РїСЂРё send."""
    strava  = get_token(db_user_id, "strava")
    whoop   = get_token(db_user_id, "whoop")
    garmin  = get_token(db_user_id, "garmin")
    coros   = get_token(db_user_id, "coros")
    polar   = get_token(db_user_id, "polar")
    profile = get_user_profile(db_user_id)
    profile_ok   = bool(profile and profile.get("vo2max"))
    fitness_ok   = bool(strava or garmin or coros or polar)
    recovery_ok  = bool(whoop or garmin or coros or polar)
    all_set      = profile_ok and fitness_ok and recovery_ok

    if all_set:
        status_lines = ["вњ… РџСЂРѕС„РёР»СЊ Р·Р°РїРѕР»РЅРµРЅ"]
        if strava:
            status_lines.append("вњ… Strava РїРѕРґРєР»СЋС‡РµРЅР°")
        if garmin:
            status_lines.append("вњ… Garmin РїРѕРґРєР»СЋС‡С‘РЅ")
        if whoop:
            status_lines.append("вњ… Whoop РїРѕРґРєР»СЋС‡С‘РЅ")

        fitness_src   = "CTL/ATL/TSB (Strava)" if strava else "Training Load (Garmin)"
        recovery_name = "Whoop" if whoop else "Garmin"

        text = (
            f"РџСЂРёРІРµС‚, {user.first_name}! рџ‘‹\n\n"
            + "\n".join(status_lines) + "\n\n"
            "Р§С‚Рѕ СѓРјРµСЋ:\n"
            f"рџЏѓ РђРЅР°Р»РёР·РёСЂСѓСЋ С„РѕСЂРјСѓ ({fitness_src}), РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёРµ Рё СЂРµРєРѕРјРµРЅРґСѓСЋ РіСЂСѓРїРїСѓ "
            "РґР»СЏ С‚СЂРµРЅРёСЂРѕРІРєРё РІС‚/РїС‚ СЃ РїСЂРѕС†РµРЅС‚РЅРѕР№ С€РєР°Р»РѕР№ РїРѕРґС…РѕРґРёРјРѕСЃС‚Рё\n"
            "рџ•ђ РўРѕ Р¶Рµ СЃР°РјРѕРµ РґР»СЏ РІРѕСЃРєСЂРµСЃРЅРѕРіРѕ Long Run СЃ СЂРµРєРѕРјРµРЅРґР°С†РёРµР№ СЃС‚СЂР°С‚РµРіРёРё "
            "(СЂРѕРІРЅС‹Р№ С‚РµРјРї РёР»Рё РїСЂРѕРіСЂРµСЃСЃРёСЏ)\n"
            f"вЂпёЏ РЈС‚СЂРѕРј РІ РґРµРЅСЊ С‚СЂРµРЅРёСЂРѕРІРєРё РїСЂРѕРІРµСЂСЏСЋ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёРµ Рё РєРѕСЂСЂРµРєС‚РёСЂСѓСЋ РїР»Р°РЅ\n"
            "рџ“ў РђРІС‚РѕРјР°С‚РёС‡РµСЃРєРё СѓРІРµРґРѕРјР»СЏСЋ РєРѕРіРґР° РІС‹С…РѕРґРёС‚ РЅРѕРІС‹Р№ Р°РЅРѕРЅСЃ С‚СЂРµРЅРёСЂРѕРІРєРё\n\n"
            "Р’С‹Р±РµСЂРё РґРµР№СЃС‚РІРёРµ рџ‘‡"
        )
    else:
        # РџРѕРєР°Р·С‹РІР°РµРј С‡РµРє-Р»РёСЃС‚ С‚РѕРіРѕ, С‡С‚Рѕ СѓР¶Рµ РїРѕРґРєР»СЋС‡РµРЅРѕ
        done = []
        if profile_ok:
            done.append("вњ… РџСЂРѕС„РёР»СЊ Р·Р°РїРѕР»РЅРµРЅ")
        if fitness_ok:
            svc_names = []
            if garmin: svc_names.append("Garmin")
            if coros:  svc_names.append("COROS")
            if polar:  svc_names.append("Polar")
            if strava: svc_names.append("Strava")
            done.append("вњ… РўСЂРµРєРµСЂ: " + ", ".join(svc_names))
        if whoop:
            done.append("вњ… Whoop РїРѕРґРєР»СЋС‡С‘РЅ")

        done_block = ("\n" + "\n".join(done) + "\n") if done else ""

        text = (
            f"РџСЂРёРІРµС‚, {user.first_name}! рџ‘‹\n"
            f"{done_block}\n"
            "РЇ РїРѕРјРѕРіСѓ РїРѕРґРіРѕС‚РѕРІРёС‚СЊСЃСЏ Рє С‚СЂРµРЅРёСЂРѕРІРєР°Рј Dusty Dumbbells.\n\n"
            "Р”Р»СЏ РЅР°С‡Р°Р»Р° СЃРґРµР»Р°Р№ РґРІР° С€Р°РіР°:\n"
            "1пёЏвѓЈ Р—Р°РїРѕР»РЅРё РїСЂРѕС„РёР»СЊ вЂ” VO2max Рё Р»Р°РєС‚Р°С‚РЅС‹Р№ РїРѕСЂРѕРі\n"
            "2пёЏвѓЈ РџРѕРґРєР»СЋС‡Рё С‚СЂРµРєРµСЂ вЂ” Garmin, COROS, Polar РёР»Рё Strava\n\n"
            "Р’С‹Р±РµСЂРё РґРµР№СЃС‚РІРёРµ рџ‘‡"
        )
        return text, _build_screen1_onboarding_keyboard()

    return text, _build_screen1_keyboard()


async def _show_main_menu(query_or_update, user, db_user_id: int):
    """РџРѕРєР°Р·С‹РІР°РµС‚ Р­РєСЂР°РЅ 1 СЂРµРґР°РєС‚РёСЂСѓСЏ С‚РµРєСѓС‰РµРµ СЃРѕРѕР±С‰РµРЅРёРµ (РґР»СЏ РЅР°РІРёРіР°С†РёРѕРЅРЅС‹С… СЌРєСЂР°РЅРѕРІ)."""
    text, keyboard = _build_main_menu_content(user, db_user_id)
    if hasattr(query_or_update, 'edit_message_text'):
        try:
            await query_or_update.edit_message_text(text, reply_markup=keyboard)
        except Exception:
            pass
    else:
        await query_or_update.message.reply_text(text, reply_markup=keyboard)


# в”Ђв”Ђ РљРћРњРђРќР”Р« в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = not user_exists(user.id)
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    if is_new:
        total = len(get_all_users())
        uname = f" (@{user.username})" if user.username else ""
        await _notify_admin(
            context.bot,
            f"рџ‘¤ РќРѕРІС‹Р№ РїРѕР»СЊР·РѕРІР°С‚РµР»СЊ: {user.full_name}{uname}\n"
            f"Р’СЃРµРіРѕ РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№: {total}"
        )
    await _show_main_menu(update, user, db_user_id)


async def cmd_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    log_activity(db_user_id, '/workout')
    msg = await update.message.reply_text("рџ”Ќ РџРѕРґР±РёСЂР°СЋ С‚СЂРµРЅРёСЂРѕРІРєСѓ...")
    await _send_recommendation(user.id, user.full_name, context, long=False, msg=msg)


async def cmd_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    log_activity(db_user_id, '/long')
    msg = await update.message.reply_text("рџ”Ќ РџРѕРґР±РёСЂР°СЋ Long Run...")
    await _send_recommendation(user.id, user.full_name, context, long=True, msg=msg)


async def cmd_morning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    log_activity(db_user_id, '/morning')
    msg = await update.message.reply_text("вЂпёЏ РџСЂРѕРІРµСЂСЏСЋ С‚РІРѕС‘ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёРµ...")
    await _send_morning_check(user.id, context, msg)


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """РџСЂРёРЅСѓРґРёС‚РµР»СЊРЅРѕРµ РѕР±РЅРѕРІР»РµРЅРёРµ РєСЌС€Р° РґР°РЅРЅС‹С… Р°С‚Р»РµС‚Р°"""
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)

    access_token = await ensure_valid_token(db_user_id)
    if not access_token:
        await update.message.reply_text("вќЊ Strava РЅРµ РїРѕРґРєР»СЋС‡РµРЅР°. РЎРЅР°С‡Р°Р»Р° /connect_strava")
        return

    msg = await update.message.reply_text("вЏі РћР±РЅРѕРІР»СЏСЋ РґР°РЅРЅС‹Рµ...")
    athlete_data = await refresh_athlete_cache(db_user_id, access_token, msg)

    if athlete_data:
        load = athlete_data["training_load"]
        cache = get_athlete_cache(db_user_id)
        updated_at = cache["updated_at"] if cache else "С‚РѕР»СЊРєРѕ С‡С‚Рѕ"
        await msg.edit_text(
            f"вњ… Р”Р°РЅРЅС‹Рµ РѕР±РЅРѕРІР»РµРЅС‹!\n\n"
            f"РўСЂРµРЅРёСЂРѕРІР°РЅРЅРѕСЃС‚СЊ (CTL): {load.get('ctl', 'вЂ”')}\n"
            f"РЈСЃС‚Р°Р»РѕСЃС‚СЊ (ATL): {load.get('atl', 'вЂ”')}\n"
            f"Р¤РѕСЂРјР° (TSB): {load.get('tsb', 'вЂ”')} вЂ” {load.get('form_text', 'вЂ”')}\n"
            f"РўСЂРµРЅРґ: {load.get('trend_text', 'вЂ”')}\n\n"
            f"РћР±РЅРѕРІР»РµРЅРѕ: {updated_at}"
        )
    else:
        await msg.edit_text("вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ РѕР±РЅРѕРІРёС‚СЊ РґР°РЅРЅС‹Рµ. РџРѕРїСЂРѕР±СѓР№ РїРѕР·Р¶Рµ.")


def _fmt_workout_date(workout_date: str) -> tuple[str, str]:
    """Р’РѕР·РІСЂР°С‰Р°РµС‚ (date_fmt '27.05', weekday 'Р’С‚РѕСЂРЅРёРє')."""
    _WEEKDAYS_RU = ["РџРѕРЅРµРґРµР»СЊРЅРёРє", "Р’С‚РѕСЂРЅРёРє", "РЎСЂРµРґР°", "Р§РµС‚РІРµСЂРі", "РџСЏС‚РЅРёС†Р°", "РЎСѓР±Р±РѕС‚Р°", "Р’РѕСЃРєСЂРµСЃРµРЅСЊРµ"]
    try:
        from datetime import datetime as _dt
        dt_obj = _dt.strptime(workout_date, "%Y-%m-%d")
        return dt_obj.strftime("%d.%m"), _WEEKDAYS_RU[dt_obj.weekday()]
    except Exception:
        return workout_date, ""


def _build_simple_workout_text(workout: dict) -> str:
    """РЈРїСЂРѕС‰С‘РЅРЅРѕРµ СѓРІРµРґРѕРјР»РµРЅРёРµ РґР»СЏ РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№ Р±РµР· РїСЂРѕС„РёР»СЏ/С‚СЂРµРєРµСЂР°."""
    date_fmt, weekday = _fmt_workout_date(workout.get("workout_date", ""))
    location  = workout.get("location") or "вЂ”"
    schedule  = workout.get("schedule") or "вЂ”"
    work_text = (workout.get("work_text") or "").strip()
    groups_raw = (workout.get("groups_raw") or "").strip()

    lines = [f"рџ“ў Р—Р°РІС‚СЂР° С‚СЂРµРЅРёСЂРѕРІРєР° Dusty Dumbbells!\n"]
    lines.append(f"{weekday} {date_fmt} | рџ“Ќ {location}")
    lines.append(f"вЏ° {schedule}")
    if work_text:
        lines.append(f"\nрџ’Є {work_text}")
    if groups_raw:
        lines.append(f"\nР“СЂСѓРїРїС‹:\n{groups_raw[:400]}")
    lines.append(
        "\nРњРЅРµ РѕС‡РµРЅСЊ Р¶Р°Р»СЊ, С‡С‚Рѕ РјРѕРіСѓ С‚РѕР»СЊРєРѕ РЅР°РїРѕРјРЅРёС‚СЊ С‚РµР±Рµ Рѕ С‚СЂРµРЅРёСЂРѕРІРєРµ, "
        "РЅРѕ РЅРµ РјРѕРіСѓ РґР°С‚СЊ СЂРµРєРѕРјРµРЅРґР°С†РёР№ Рѕ РїРѕРіРѕРґРµ, СЂР°Р·РјРёРЅРєРµ, РіСЂСѓРїРїРµ, РїРёС‚Р°РЅРёРё Рё СЃС‚СЂР°С‚РµРіРёРё. рџ¤·"
    )
    lines.append(
        "\nР§С‚РѕР±С‹ РїРѕР»СѓС‡РёС‚СЊ РїРѕР»РЅС‹Р№ Р°РЅР°Р»РёР· вЂ” Р·Р°РїРѕР»РЅРё РїСЂРѕС„РёР»СЊ Рё РїРѕРґРєР»СЋС‡Рё С‚СЂРµРєРµСЂ:\n"
        "рџ‘¤ /profile вЂ” VO2max Рё Р»Р°РєС‚Р°С‚РЅС‹Р№ РїРѕСЂРѕРі\n"
        "рџ”— Garmin, COROS РёР»Рё Polar вЂ” /connect_garmin, /connect_coros"
    )
    return "\n".join(lines)


def _build_status_text(db_user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    strava = get_token(db_user_id, "strava")
    whoop = get_token(db_user_id, "whoop")
    garmin = get_token(db_user_id, "garmin")
    cache = get_athlete_cache(db_user_id)
    prefs = get_preferences(db_user_id)
    use_garmin_rec = prefs.get("use_garmin_recovery", True) if prefs else True

    lines = ["РџРѕРґРєР»СЋС‡С‘РЅРЅС‹Рµ СЃРµСЂРІРёСЃС‹:\n"]
    lines.append(f"{'вњ…' if strava else 'вќЊ'} Strava")
    lines.append(f"{'вњ…' if whoop else 'вќЊ'} Whoop")
    lines.append(f"{'вњ…' if garmin else 'вќЊ'} Garmin")

    if cache:
        lines.append(f"\nР”Р°РЅРЅС‹Рµ Strava: РѕР±РЅРѕРІР»РµРЅС‹ {cache['updated_at'][:10]}")
    else:
        lines.append("\nР”Р°РЅРЅС‹Рµ Strava: РЅРµ Р·Р°РіСЂСѓР¶РµРЅС‹ (/refresh)")

    # РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С… РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ
    lines.append("\nРСЃС‚РѕС‡РЅРёРє РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ:")
    if whoop:
        lines.append("  Whoop (РїСЂРёРѕСЂРёС‚РµС‚)")
        if garmin:
            lines.append("  Garmin вЂ” СЂРµР·РµСЂРІ РµСЃР»Рё Whoop РЅРµРґРѕСЃС‚СѓРїРµРЅ")
    elif garmin:
        if use_garmin_rec:
            lines.append("  Garmin (Body Battery, HRV)")
        else:
            lines.append("  Garmin РїРѕРґРєР»СЋС‡С‘РЅ, РЅРѕ РґР°РЅРЅС‹Рµ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ РѕС‚РєР»СЋС‡РµРЅС‹")
    else:
        lines.append("  РќРµС‚ РґР°РЅРЅС‹С… (РїРѕРґРєР»СЋС‡Рё Whoop РёР»Рё Garmin)")

    keyboard = None
    if garmin and not whoop:
        toggle_label = "РћС‚РєР»СЋС‡РёС‚СЊ Garmin РґР»СЏ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ" if use_garmin_rec else "Р’РєР»СЋС‡РёС‚СЊ Garmin РґР»СЏ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(toggle_label, callback_data="toggle_garmin_recovery")
        ]])

    if not strava:
        lines.append("\nРџРѕРґРєР»СЋС‡Рё Strava: /connect_strava")

    last_notif = get_last_workout_notification()
    if last_notif and last_notif.get("workout_date"):
        date_fmt, weekday = _fmt_workout_date(last_notif["workout_date"])
        lines.append(f"\nРџРѕСЃР»РµРґРЅРёР№ Р°РЅРѕРЅСЃ: {weekday} {date_fmt} (СѓРІРµРґРѕРјР»РµРЅРѕ {last_notif['users_notified']} РїРѕР»СЊР·.)")

    lines.append(f"\nР’РµСЂСЃРёСЏ Р±РѕС‚Р°: {VERSION} ({BUILD_DATE})")

    return '\n'.join(lines), keyboard


async def _show_status(query_or_update, db_user_id: int):
    text, toggle_kb = _build_status_text(db_user_id)
    keyboard = _merge_keyboards(toggle_kb, get_main_keyboard())
    if hasattr(query_or_update, 'edit_message_text'):
        await query_or_update.edit_message_text(text, reply_markup=keyboard)
    else:
        await query_or_update.message.reply_text(text, reply_markup=keyboard)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    await _show_status(update, db_user_id)


async def cmd_connect_strava(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _mark_user_active_if_needed(user.id, user.full_name, user.username)
    auth_url = get_auth_url(user.id)
    keyboard = [[InlineKeyboardButton("рџ”— Р’РѕР№С‚Рё РІ Strava", url=auth_url)]]
    await update.message.reply_text(
        "вљ пёЏ Strava РІСЂРµРјРµРЅРЅРѕ РѕРіСЂР°РЅРёС‡РµРЅР° вЂ” РїРѕРґРєР»СЋС‡РµРЅРёРµ РЅРѕРІС‹С… РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№ РЅР° РїСЂРѕРІРµСЂРєРµ Сѓ Strava.\n"
        "РСЃРїРѕР»СЊР·СѓР№ Garmin РёР»Рё COROS (/connect_garmin, /connect_coros) РґР»СЏ РїРѕР»РЅРѕС†РµРЅРЅРѕР№ СЂР°Р±РѕС‚С‹.\n\n"
        "РќР°Р¶РјРё РєРЅРѕРїРєСѓ Рё Р°РІС‚РѕСЂРёР·СѓР№СЃСЏ РІ Strava.\n\n"
        "РџРѕСЃР»Рµ Р°РІС‚РѕСЂРёР·Р°С†РёРё С‚С‹ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё РїРѕР»СѓС‡РёС€СЊ СЃРѕРѕР±С‰РµРЅРёРµ РІ Telegram вЂ” РЅРёС‡РµРіРѕ РєРѕРїРёСЂРѕРІР°С‚СЊ РЅРµ РЅСѓР¶РЅРѕ.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_connect_whoop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from whoop import get_auth_url as whoop_auth_url
    user = update.effective_user
    _mark_user_active_if_needed(user.id, user.full_name, user.username)
    auth_url = whoop_auth_url(user.id)
    keyboard = [[InlineKeyboardButton("рџ”— Р’РѕР№С‚Рё РІ Whoop", url=auth_url)]]
    await update.message.reply_text(
        "РќР°Р¶РјРё РєРЅРѕРїРєСѓ Рё Р°РІС‚РѕСЂРёР·СѓР№СЃСЏ РІ Whoop.\n\n"
        "РџРѕСЃР»Рµ Р°РІС‚РѕСЂРёР·Р°С†РёРё Р±СЂР°СѓР·РµСЂ РѕС‚РєСЂРѕРµС‚ СЃС‚СЂР°РЅРёС†Сѓ СЃ JSON вЂ” "
        "СЃРєРѕРїРёСЂСѓР№ РІРµСЃСЊ URL РёР· Р°РґСЂРµСЃРЅРѕР№ СЃС‚СЂРѕРєРё Рё РѕС‚РїСЂР°РІСЊ РјРЅРµ СЃСЋРґР°.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["awaiting_whoop_code"] = True


async def cmd_connect_garmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _mark_user_active_if_needed(update.effective_user.id, update.effective_user.full_name, update.effective_user.username)
    await update.message.reply_text(
        "РџРѕРґРєР»СЋС‡РµРЅРёРµ Garmin Connect\n\n"
        "Email Рё РїР°СЂРѕР»СЊ С…СЂР°РЅСЏС‚СЃСЏ РЅР° СЃРµСЂРІРµСЂРµ РІ Р·Р°С€РёС„СЂРѕРІР°РЅРЅРѕРј РІРёРґРµ (AES-256) вЂ” "
        "РІ РѕС‚РєСЂС‹С‚РѕРј РІРёРґРµ РѕРЅРё РЅРёРіРґРµ РЅРµ СЃРѕС…СЂР°РЅСЏСЋС‚СЃСЏ.\n\n"
        "Р’РІРµРґРё email РѕС‚ Garmin Connect:"
    )
    context.user_data["awaiting_garmin"] = "email"


async def cmd_connect_coros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _mark_user_active_if_needed(update.effective_user.id, update.effective_user.full_name, update.effective_user.username)
    await update.message.reply_text(
        "РџРѕРґРєР»СЋС‡РµРЅРёРµ COROS\n\n"
        "Email Рё РїР°СЂРѕР»СЊ С…СЂР°РЅСЏС‚СЃСЏ РЅР° СЃРµСЂРІРµСЂРµ РІ Р·Р°С€РёС„СЂРѕРІР°РЅРЅРѕРј РІРёРґРµ (AES-256) вЂ” "
        "РІ РѕС‚РєСЂС‹С‚РѕРј РІРёРґРµ РѕРЅРё РЅРёРіРґРµ РЅРµ СЃРѕС…СЂР°РЅСЏСЋС‚СЃСЏ.\n\n"
        "Р’РІРµРґРё email РѕС‚ Р°РєРєР°СѓРЅС‚Р° COROS:"
    )
    context.user_data["awaiting_coros"] = "email"


async def cmd_connect_polar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from polar import get_auth_url as polar_auth_url
    user = update.effective_user
    _mark_user_active_if_needed(user.id, user.full_name, user.username)
    auth_url = polar_auth_url(user.id)
    if not auth_url:
        await update.message.reply_text(
            "вќЊ Polar РЅРµ РЅР°СЃС‚СЂРѕРµРЅ. РћР±СЂР°С‚РёС‚РµСЃСЊ Рє Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂСѓ."
        )
        return
    keyboard = [[InlineKeyboardButton("рџ”— Р’РѕР№С‚Рё РІ Polar", url=auth_url)]]
    await update.message.reply_text(
        "РџРѕРґРєР»СЋС‡РµРЅРёРµ Polar\n\n"
        "РќР°Р¶РјРё РєРЅРѕРїРєСѓ Рё Р°РІС‚РѕСЂРёР·СѓР№СЃСЏ РІ Polar Flow.\n\n"
        "РџРѕСЃР»Рµ Р°РІС‚РѕСЂРёР·Р°С†РёРё С‚С‹ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё РїРѕР»СѓС‡РёС€СЊ СЃРѕРѕР±С‰РµРЅРёРµ РІ Telegram вЂ” РЅРёС‡РµРіРѕ РєРѕРїРёСЂРѕРІР°С‚СЊ РЅРµ РЅСѓР¶РЅРѕ.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """РџРѕРєР°Р·С‹РІР°РµС‚ РїРѕСЃР»РµРґРЅРёР№ РїСЂРѕРјРїС‚ (С‚РѕР»СЊРєРѕ РґР»СЏ Р°РґРјРёРЅРѕРІ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("РќРµС‚ РґРѕСЃС‚СѓРїР°.")
        return
    prompt = claude_advisor.last_prompt
    if not prompt:
        await update.message.reply_text("РџСЂРѕРјРїС‚ РµС‰С‘ РЅРµ РѕС‚РїСЂР°РІР»СЏР»СЃСЏ. РЎРЅР°С‡Р°Р»Р° /workout.")
        return
    text = f"РџРѕСЃР»РµРґРЅРёР№ РїСЂРѕРјРїС‚ ({len(prompt)} СЃРёРјРІ.):\n\n{prompt}"
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


def _vo2max_tag(profile: dict) -> str:
    source = profile.get("vo2max_source")
    updated = profile.get("vo2max_updated_at") or profile.get("updated_at") or ""
    date_str = updated[:10] if updated else ""
    if source == "garmin":
        from datetime import datetime as _dt
        try:
            days = (_dt.now() - _dt.fromisoformat(updated)).days if updated else 999
        except Exception:
            days = 999
        if days > 30:
            return f"Garmin В· {date_str} В· СѓСЃС‚Р°СЂРµР»Рѕ"
        return f"Garmin В· {date_str}"
    if source == "manual":
        return "РІСЂСѓС‡РЅСѓСЋ"
    return "РІСЂСѓС‡РЅСѓСЋ" if updated else ""


SPECIALIZATIONS = {
    "5k": "5 РєРј",
    "10k": "10 РєРј",
    "half_marathon": "РџРѕР»СѓРјР°СЂР°С„РѕРЅ",
    "marathon": "РњР°СЂР°С„РѕРЅ",
    "speed": "Р Р°Р·РІРёС‚РёРµ СЃРєРѕСЂРѕСЃС‚Рё",
    "fitness": "РћР±С‰Р°СЏ С„РѕСЂРјР°",
}


def _build_profile_text(profile: dict | None) -> str:
    if not profile or not any([profile.get("vo2max"), profile.get("lactate_threshold_pace"), profile.get("gender")]):
        return "РџСЂРѕС„РёР»СЊ РЅРµ Р·Р°РїРѕР»РЅРµРЅ. РСЃРїРѕР»СЊР·СѓР№ РєРЅРѕРїРєРё РЅРёР¶Рµ С‡С‚РѕР±С‹ РґРѕР±Р°РІРёС‚СЊ РґР°РЅРЅС‹Рµ."
    lines = ["РўРІРѕР№ РїСЂРѕС„РёР»СЊ:\n"]
    if profile.get("gender"):
        lines.append(f"РџРѕР»: {'РњСѓР¶СЃРєРѕР№' if profile['gender'] == 'male' else 'Р–РµРЅСЃРєРёР№'}")
    if profile.get("vo2max"):
        tag = _vo2max_tag(profile)
        vo2_lock = " рџ”’" if profile.get("vo2max_locked") else ""
        lines.append(f"VO2max: {profile['vo2max']} РјР»/РєРі/РјРёРЅ{f'  ({tag})' if tag else ''}{vo2_lock}")
    if profile.get("lactate_threshold_pace"):
        lt = f"Р›Р°РєС‚Р°С‚РЅС‹Р№ РїРѕСЂРѕРі: {profile['lactate_threshold_pace']} РјРёРЅ/РєРј"
        if profile.get("lactate_threshold_hr"):
            lt += f" РїСЂРё Р§РЎРЎ {profile['lactate_threshold_hr']} СѓРґ/РјРёРЅ"
        lt_source = profile.get("lactate_source")
        lt_lock = " рџ”’" if profile.get("lactate_locked") else ""
        if lt_source:
            lt += f"  ({'РІСЂСѓС‡РЅСѓСЋ' if lt_source == 'manual' else 'РёР· СЃРµСЂРІРёСЃР°'}){lt_lock}"
        elif lt_lock:
            lt += f"  {lt_lock.strip()}"
        lines.append(lt)
    spec = profile.get("specialization")
    spec_label = SPECIALIZATIONS.get(spec) if spec else None
    lines.append(f"РЎРїРµС†РёР°Р»РёР·Р°С†РёСЏ: {spec_label or 'РџРѕР»СѓРјР°СЂР°С„РѕРЅ (РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ)'}")
    if profile.get("updated_at"):
        lines.append(f"\nРћР±РЅРѕРІР»РµРЅРѕ: {profile['updated_at'][:10]}")
    return '\n'.join(lines)


def _build_profile_keyboard(profile: dict | None = None) -> InlineKeyboardMarkup:
    p = profile or {}
    vo2_locked = bool(p.get("vo2max_locked"))
    lt_locked = bool(p.get("lactate_locked"))
    rows = [
        [InlineKeyboardButton("рџ“Љ РЈРєР°Р·Р°С‚СЊ VO2max",   callback_data="profile_set_vo2max"),
         InlineKeyboardButton("рџЏѓ Р›Р°РєС‚Р°С‚РЅС‹Р№ РїРѕСЂРѕРі", callback_data="profile_set_lactate")],
        [InlineKeyboardButton("рџ‘¤ РџРѕР»", callback_data="profile_set_gender"),
         InlineKeyboardButton("рџЋЇ РЎРїРµС†РёР°Р»РёР·Р°С†РёСЏ", callback_data="profile_set_specialization")],
    ]
    # РўСѓРјР±Р»РµСЂС‹ Р±Р»РѕРєРёСЂРѕРІРєРё вЂ” С‚РѕР»СЊРєРѕ РµСЃР»Рё Р·РЅР°С‡РµРЅРёРµ Р·Р°РґР°РЅРѕ
    lock_row = []
    if p.get("vo2max"):
        lbl = "рџ”’ VO2max (РЅРµ РѕР±РЅРѕРІР»СЏС‚СЊ)" if vo2_locked else "рџ”“ VO2max (РѕР±РЅРѕРІР»СЏС‚СЊ)"
        lock_row.append(InlineKeyboardButton(lbl, callback_data="profile_toggle_vo2max_lock"))
    if p.get("lactate_threshold_pace"):
        lbl = "рџ”’ Р›Рџ (РЅРµ РѕР±РЅРѕРІР»СЏС‚СЊ)" if lt_locked else "рџ”“ Р›Рџ (РѕР±РЅРѕРІР»СЏС‚СЊ)"
        lock_row.append(InlineKeyboardButton(lbl, callback_data="profile_toggle_lactate_lock"))
    if lock_row:
        rows.append(lock_row)
    rows.append(_settings_nav())
    return InlineKeyboardMarkup(rows)


def _build_specialization_keyboard(current_spec: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"{'вњ… ' if key == current_spec else ''}{label}",
            callback_data=f"spec_set_{key}")]
        for key, label in SPECIALIZATIONS.items()
    ]
    rows.append([InlineKeyboardButton("в†ђ РќР°Р·Р°Рґ", callback_data="my_profile")])
    return InlineKeyboardMarkup(rows)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    profile = get_user_profile(db_user_id)
    await update.message.reply_text(
        _build_profile_text(profile),
        reply_markup=_build_profile_keyboard(profile)
    )


# Р РµР¶РёРј С„РѕСЂРјРёСЂРѕРІР°РЅРёСЏ Р Р•РљРћРњР•РќР”РђР¦РР (РЁР°Рі 2). РђРЅР°Р»РёР· Р°РЅРѕРЅСЃР° (РЁР°Рі 1) РІСЃРµРіРґР° deep (Р°РґРјРёРЅ).
_MODE_INFO = {
    "deep":  ("рџ§ ", "Р“Р»СѓР±РѕРєРёР№ (РР)", "~2 РјРёРЅ",     "РР С„РѕСЂРјСѓР»РёСЂСѓРµС‚ СЂРµРєРѕРјРµРЅРґР°С†РёСЋ, РјР°РєСЃ. РєР°С‡РµСЃС‚РІРѕ"),
    "smart": ("вљЎ", "Р‘С‹СЃС‚СЂС‹Р№ (РР)",  "~30-60 СЃРµРє", "Р±Р°Р»Р°РЅСЃ РєР°С‡РµСЃС‚РІР° Рё СЃРєРѕСЂРѕСЃС‚Рё"),
    "fast":  ("рџЄ¶", "Р›С‘РіРєРёР№ (РР)",   "~10 СЃРµРє",    "РєРѕСЂРѕС‚РєРѕРµ РР-РѕР±СЉСЏСЃРЅРµРЅРёРµ"),
    "calc":  ("рџ“Љ", "Р Р°СЃС‡С‘С‚РЅС‹Р№",      "С„РѕСЂРјСѓР»С‹",    "РіСЂСѓРїРїР° Рё % РїРѕ С„РѕСЂРјСѓР»Р°Рј, С‚РµРєСЃС‚ РєРѕСЂРѕС‚РєРѕ РѕС‚ РР"),
}


def _build_mode_text(current_mode: str) -> str:
    lines = ["рџ§  Р РµР¶РёРј СЂРµРєРѕРјРµРЅРґР°С†РёРё (РєР°Рє Р±РѕС‚ С„РѕСЂРјСѓР»РёСЂСѓРµС‚ СЃРѕРІРµС‚ РїРѕ РіСЂСѓРїРїРµ):\n"]
    for key, (emoji, label, timing, desc) in _MODE_INFO.items():
        mark = "вњ… " if key == current_mode else "   "
        lines.append(f"{mark}{emoji} {label} ({timing}) вЂ” {desc}")
    lines.append("\nР§РёСЃР»Р° (РіСЂСѓРїРїР°, %, Р·РѕРЅС‹) РІСЃРµРіРґР° СЃС‡РёС‚Р°СЋС‚СЃСЏ С„РѕСЂРјСѓР»Р°РјРё; СЂРµР¶РёРј РІР»РёСЏРµС‚ РЅР° С‚Рѕ,\n"
                 "РЅР°СЃРєРѕР»СЊРєРѕ РіР»СѓР±РѕРєРѕ РР РѕР±СЉСЏСЃРЅСЏРµС‚ Рё С„РѕСЂРјСѓР»РёСЂСѓРµС‚ С‚РµРєСЃС‚. Р’С‹Р±РµСЂРё СЂРµР¶РёРј:")
    return "\n".join(lines)


def _build_mode_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    def btn(key):
        emoji, label, _timing, _ = _MODE_INFO[key]
        mark = "вњ… " if key == current_mode else ""
        return InlineKeyboardButton(f"{mark}{emoji} {label}", callback_data=f"mode_set_{key}")
    return InlineKeyboardMarkup([
        [btn("deep")],
        [btn("smart")],
        [btn("fast")],
        [btn("calc")],
    ])


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    prefs = get_preferences(db_user_id)
    current_mode = prefs.get("ai_mode", "smart") if prefs else "smart"
    await update.message.reply_text(
        _build_mode_text(current_mode),
        reply_markup=_build_mode_keyboard(current_mode)
    )


def _build_notifications_text(prefs: dict) -> str:
    def mark(key): return "вњ…" if (prefs or {}).get(key, True) else "вќЊ"
    return (
        "рџ”” РќР°СЃС‚СЂРѕР№РєРё СѓРІРµРґРѕРјР»РµРЅРёР№:\n\n"
        f"{mark('notify_interval')} РўСЂРµРЅРёСЂРѕРІРєРё РІС‚/РїС‚\n"
        f"{mark('notify_interval_extra')} РќРѕРІС‹Рµ РіСЂСѓРїРїС‹\n"
        f"{mark('notify_long')} Р’РѕСЃРєСЂРµСЃРЅС‹Р№ Long Run"
    )


def _build_notifications_keyboard(prefs: dict) -> InlineKeyboardMarkup:
    def lbl(key, title):
        on = (prefs or {}).get(key, True)
        action = f"notif_off_{key}" if on else f"notif_on_{key}"
        return InlineKeyboardButton(f"{'вњ…' if on else 'вќЊ'} {title} вЂ” {'[Р’С‹РєР»]' if on else '[Р’РєР»]'}", callback_data=action)
    return InlineKeyboardMarkup([
        [lbl("notify_interval", "РўСЂРµРЅРёСЂРѕРІРєРё РІС‚/РїС‚")],
        [lbl("notify_interval_extra", "РќРѕРІС‹Рµ РіСЂСѓРїРїС‹")],
        [lbl("notify_long", "Р’РѕСЃРєСЂРµСЃРЅС‹Р№ Long Run")],
        _settings_nav(),
    ])


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    prefs = get_preferences(db_user_id)
    await update.message.reply_text(
        _build_notifications_text(prefs),
        reply_markup=_build_notifications_keyboard(prefs),
    )


def _build_help_text(is_admin: bool) -> str:
    text = (
        f"Dusty Dumbbells Running Bot  v{VERSION}\n\n"
        "РљРѕРјР°РЅРґС‹:\n"
        "/start вЂ” РіР»Р°РІРЅРѕРµ РјРµРЅСЋ\n"
        "/workout вЂ” СЂРµРєРѕРјРµРЅРґР°С†РёСЏ РіСЂСѓРїРїС‹ РґР»СЏ РІС‚/РїС‚ С‚СЂРµРЅРёСЂРѕРІРєРё\n"
        "/long вЂ” СЂРµРєРѕРјРµРЅРґР°С†РёСЏ РґР»СЏ РІРѕСЃРєСЂРµСЃРЅРѕРіРѕ Long Run\n"
        "/morning вЂ” СѓС‚СЂРµРЅРЅСЏСЏ РїСЂРѕРІРµСЂРєР° РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ\n"
        "/status вЂ” СЃС‚Р°С‚СѓСЃ РїРѕРґРєР»СЋС‡С‘РЅРЅС‹С… СЃРµСЂРІРёСЃРѕРІ\n"
        "/refresh вЂ” РѕР±РЅРѕРІРёС‚СЊ РґР°РЅРЅС‹Рµ РёР· Strava\n"
        "/profile вЂ” РїСЂРѕС„РёР»СЊ (VO2max, Р»Р°РєС‚Р°С‚РЅС‹Р№ РїРѕСЂРѕРі)\n"
        "/mode вЂ” СЂРµР¶РёРј РР (Р±С‹СЃС‚СЂС‹Р№ / РіР»СѓР±РѕРєРёР№)\n"
        "/notifications вЂ” РЅР°СЃС‚СЂРѕР№РєРё СѓРІРµРґРѕРјР»РµРЅРёР№\n"
        "/connect_strava вЂ” РїРѕРґРєР»СЋС‡РёС‚СЊ Strava\n"
        "/connect_garmin вЂ” РїРѕРґРєР»СЋС‡РёС‚СЊ Garmin Connect\n"
        "/connect_whoop вЂ” РїРѕРґРєР»СЋС‡РёС‚СЊ Whoop\n"
        "/connect_coros вЂ” РїРѕРґРєР»СЋС‡РёС‚СЊ COROS\n"
        "/connect_polar вЂ” РїРѕРґРєР»СЋС‡РёС‚СЊ Polar\n"
        "/feedback вЂ” РѕР±СЂР°С‚РЅР°СЏ СЃРІСЏР·СЊ (РїСЂРѕР±Р»РµРјР° / РёРґРµСЏ)\n"
        "/help вЂ” СЌС‚Р° СЃРїСЂР°РІРєР°\n\n"
        "РђРІС‚РѕРјР°С‚РёС‡РµСЃРєРёРµ СѓРІРµРґРѕРјР»РµРЅРёСЏ:\n"
        "вЂў РќР°РєР°РЅСѓРЅРµ С‚СЂРµРЅРёСЂРѕРІРєРё (РїРЅ, С‡С‚, СЃР±) РІ 20:00 РњРЎРљ\n"
        "вЂў РЈС‚СЂРѕРј РІ РґРµРЅСЊ С‚СЂРµРЅРёСЂРѕРІРєРё РІ 07:00 РњРЎРљ"
    )
    if is_admin:
        text += (
            "\n\nвЂ” РђРґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ вЂ”\n"
            "/stats вЂ” СЃС‚Р°С‚РёСЃС‚РёРєР° РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№ Рё Р°РєС‚РёРІРЅРѕСЃС‚Рё\n"
            "/users вЂ” СЃРїРёСЃРѕРє РІСЃРµС… РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№\n"
            "/services вЂ” РїРѕР»СЊР·РѕРІР°С‚РµР»Рё РїРѕ РїРѕРґРєР»СЋС‡С‘РЅРЅС‹Рј СЃРµСЂРІРёСЃР°Рј\n"
            "/prompt вЂ” РїРѕСЃР»РµРґРЅРёР№ РїСЂРѕРјРїС‚ Рє РјРѕРґРµР»Рё\n"
            "/debug вЂ” СЂР°Р·Р±РѕСЂ РїРѕСЃР»РµРґРЅРµР№ С‚СЂРµРЅРёСЂРѕРІРєРё\n"
            "/debug_long вЂ” СЂР°Р·Р±РѕСЂ РїРѕСЃР»РµРґРЅРµРіРѕ Long Run\n"
            "/ratings вЂ” РїРѕСЃР»РµРґРЅРёРµ РѕС†РµРЅРєРё СЂРµРєРѕРјРµРЅРґР°С†РёР№\n"
            "/feedbacks вЂ” РїРѕСЃР»РµРґРЅРёРµ СЃРѕРѕР±С‰РµРЅРёСЏ РѕР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё\n"
            "/analyze вЂ” Р°РЅР°Р»РёР· РїРѕСЃР»РµРґРЅРµР№ С‚СЂРµРЅРёСЂРѕРІРєРё С‡РµСЂРµР· DeepSeek\n"
            "/preprocess_mode вЂ” СЂРµР¶РёРј Р°РЅР°Р»РёР·Р° С‚СЂРµРЅРёСЂРѕРІРѕРє (deep/smart)\n"
            "/test_workout вЂ” С‚РµСЃС‚ РЁР°РіР° 2 (СЂРµРєРѕРјРµРЅРґР°С†РёСЏ РіСЂСѓРїРїС‹) РЅР° С‚РІРѕРёС… РґР°РЅРЅС‹С…\n"
            "/test_long вЂ” С‚РµСЃС‚ РЁР°РіР° 2 РґР»СЏ РґР»РёС‚РµР»СЊРЅРѕР№ РЅР° С‚РІРѕРёС… РґР°РЅРЅС‹С…\n"
            "/reanalyze вЂ” С„РѕСЂСЃ РїРµСЂРµР°РЅР°Р»РёР·Р° СЃРІРµР¶РёС… Р°РЅРѕРЅСЃРѕРІ (РѕР±РЅРѕРІРёС‚СЊ РєСЌС€)"
        )
    return text


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _mark_user_active_if_needed(user.id, user.full_name, user.username)
    is_admin = user.id in ADMIN_TELEGRAM_IDS
    await update.message.reply_text(_build_help_text(is_admin), reply_markup=get_main_keyboard())


def _build_feedback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("рџђ› РџСЂРѕР±Р»РµРјР°",  callback_data="feedback_bug"),
         InlineKeyboardButton("рџ’Ў РРґРµСЏ",      callback_data="feedback_feature")],
        [InlineKeyboardButton("рџЏ  Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ", callback_data="main_menu")],
    ])


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _mark_user_active_if_needed(user.id, user.full_name, user.username)
    await update.message.reply_text("Р’С‹Р±РµСЂРё С‚РёРї:", reply_markup=_build_feedback_keyboard())


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """РЎС‚Р°С‚РёСЃС‚РёРєР° Р±РѕС‚Р° (С‚РѕР»СЊРєРѕ РґР»СЏ Р°РґРјРёРЅРѕРІ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("РќРµС‚ РґРѕСЃС‚СѓРїР°.")
        return
    s = get_bot_stats()
    text = (
        "рџ“Љ <b>РЎС‚Р°С‚РёСЃС‚РёРєР° Р±РѕС‚Р°</b>\n\n"
        f"рџ‘Ґ РџРѕР»СЊР·РѕРІР°С‚РµР»Рё: {s['total']}\n"
        f"вњ… РђРєС‚РёРІРЅС‹С…: {s.get('active_bot', s['total'])}\n"
        f"рџ’¤ РќРµР°РєС‚РёРІРЅС‹С…: {s.get('inactive_bot', 0)}\n"
        f"РќРѕРІС‹С… Р·Р° 7 РґРЅРµР№: {s['new_7d']}\n"
        f"РђРєС‚РёРІРЅС‹С… Р·Р° 7 РґРЅРµР№: {s['active_7d']}\n\n"
        "РџРѕРґРєР»СЋС‡РµРЅРёСЏ:\n"
        f"рџџ  Strava: {s['strava']}\n"
        f"вљЄ Whoop: {s['whoop']}\n"
        f"рџ”µ Garmin: {s['garmin']}\n"
        f"рџ”ґ COROS: {s['coros']}\n"
        f"вќ„пёЏ Polar: {s.get('polar', 0)}\n"
        f"рџ‘¤ РџСЂРѕС„РёР»СЊ Р·Р°РїРѕР»РЅРµРЅ: {s['profile']}\n\n"
        "Р—Р°РїСЂРѕСЃС‹ Р·Р° 7 РґРЅРµР№:\n"
        f"рџ“‹ /workout: {s['workout_7d']}\n"
        f"рџ•ђ /long: {s['long_7d']}\n"
        f"вЂпёЏ /morning: {s['morning_7d']}\n\n"
        f"в­ђ РЎСЂРµРґРЅСЏСЏ РѕС†РµРЅРєР°: {s.get('avg_rating') or 'вЂ”'}/10 (Р·Р° 30 РґРЅРµР№)\n"
        f"рџ“Љ РћС†РµРЅРѕРє РїРѕР»СѓС‡РµРЅРѕ: {s.get('ratings_30d', 0)}\n"
        f"рџ’¬ РћР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё: {s.get('feedback_total', 0)} "
        f"(Р±Р°РіРё: {s.get('feedback_bugs', 0)}, РёРґРµРё: {s.get('feedback_features', 0)})"
    )
    await update.message.reply_text(text, parse_mode="HTML")


def _fmt_user_ref(name: str | None, username: str | None) -> str:
    """@username РµСЃР»Рё РµСЃС‚СЊ, РёРЅР°С‡Рµ РёРјСЏ."""
    if username:
        return f"@{username}"
    return name or "вЂ”"


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """РЎРїРёСЃРѕРє РІСЃРµС… РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№ (С‚РѕР»СЊРєРѕ РґР»СЏ Р°РґРјРёРЅРѕРІ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("РќРµС‚ РґРѕСЃС‚СѓРїР°.")
        return

    from datetime import datetime as _dt
    users = get_all_users_with_details()

    lines = [f"рџ‘Ґ Р’СЃРµ РїРѕР»СЊР·РѕРІР°С‚РµР»Рё ({len(users)}):"]
    for i, (_, tid, name, uname, created_at) in enumerate(users, 1):
        try:
            date_fmt = _dt.fromisoformat(created_at).strftime("%d.%m")
        except Exception:
            date_fmt = "вЂ”"
        name_str = name or "вЂ”"
        uname_str = f" (@{uname})" if uname else ""
        lines.append(f"{i}. {name_str}{uname_str} вЂ” {date_fmt}")

    text = "\n".join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """РЎРїРёСЃРѕРє РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№ РїРѕ РїРѕРґРєР»СЋС‡С‘РЅРЅС‹Рј СЃРµСЂРІРёСЃР°Рј (С‚РѕР»СЊРєРѕ РґР»СЏ Р°РґРјРёРЅРѕРІ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("РќРµС‚ РґРѕСЃС‚СѓРїР°.")
        return

    all_users = get_all_users_with_details()
    all_tids_ordered = [(tid, name, uname) for _, tid, name, uname, _ in all_users]

    service_defs = [
        ("strava", "рџџ  Strava"),
        ("whoop",  "вљЄ Whoop"),
        ("garmin", "рџ”µ Garmin"),
        ("coros",  "рџ”ґ COROS"),
        ("polar",  "вќ„пёЏ Polar"),
    ]

    service_users: dict[str, list] = {}
    any_service_tids: set[int] = set()

    for svc, _ in service_defs:
        rows = get_users_with_service_full(svc)
        service_users[svc] = rows
        any_service_tids |= {r[0] for r in rows}

    profile_tids = {r[0] for r in get_users_with_profile_full()}

    only_profile = [(tid, n, u) for tid, n, u in all_tids_ordered
                    if tid in profile_tids and tid not in any_service_tids]
    nothing      = [(tid, n, u) for tid, n, u in all_tids_ordered
                    if tid not in profile_tids and tid not in any_service_tids]

    lines = ["рџ“Љ РџРѕР»СЊР·РѕРІР°С‚РµР»Рё РїРѕ СЃРµСЂРІРёСЃР°Рј:\n"]
    for svc, label in service_defs:
        rows = service_users[svc]
        refs = ", ".join(_fmt_user_ref(n, u) for _, n, u in rows)
        lines.append(f"{label} ({len(rows)}): {refs or 'вЂ”'}")

    refs = ", ".join(_fmt_user_ref(n, u) for _, n, u in only_profile)
    lines.append(f"\nрџ‘¤ РўРѕР»СЊРєРѕ РїСЂРѕС„РёР»СЊ ({len(only_profile)}): {refs or 'вЂ”'}")

    refs = ", ".join(_fmt_user_ref(n, u) for _, n, u in nothing)
    lines.append(f"вќЊ РќРёС‡РµРіРѕ РЅРµ РїРѕРґРєР»СЋС‡РµРЅРѕ ({len(nothing)}): {refs or 'вЂ”'}")

    inactive = get_inactive_users()
    if inactive:
        refs = ", ".join(_fmt_user_ref(n, u) for _, n, u in inactive)
        lines.append(f"\nрџ’¤ РќРµР°РєС‚РёРІРЅС‹С… (Р·Р°Р±Р»РѕРєРёСЂРѕРІР°Р»Рё Р±РѕС‚Р°) ({len(inactive)}): {refs}")

    text = "\n".join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """РџРѕРєР°Р·С‹РІР°РµС‚ СЂР°СЃРїР°СЂСЃРµРЅРЅС‹Рµ РґР°РЅРЅС‹Рµ РїРѕСЃР»РµРґРЅРµР№ С‚СЂРµРЅРёСЂРѕРІРєРё (С‚РѕР»СЊРєРѕ РґР»СЏ Р°РґРјРёРЅРѕРІ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("РќРµС‚ РґРѕСЃС‚СѓРїР°.")
        return
    if not last_workout:
        await update.message.reply_text("РўСЂРµРЅРёСЂРѕРІРєР° РµС‰С‘ РЅРµ Р·Р°РіСЂСѓР¶Р°Р»Р°СЃСЊ. РЎРЅР°С‡Р°Р»Р° /workout.")
        return

    w = last_workout
    lines = [
        f"рџ“… {w.get('weekday', '').capitalize()} {w.get('workout_date', '')}",
        f"РўРёРї: {w.get('workout_type', 'вЂ”')}",
        f"рџ“Ќ {w.get('location', 'вЂ”')}",
        f"вЏ° {w.get('schedule', 'вЂ”')}",
        f"рџ“Џ РћР±СЉС‘Рј: {w.get('total_volume_km', 'вЂ”')}",
        f"is_past: {w.get('is_past', False)}",
        "",
        "Р РђР‘РћРўРђ:",
        w.get('work_text', 'вЂ”') or 'вЂ”',
        "",
        "Р“Р РЈРџРџР«:",
        w.get('groups_raw', 'вЂ”') or 'вЂ”',
    ]
    extra = w.get('extra_groups', [])
    if extra:
        nums = ', '.join(g['number'] for g in extra)
        lines += ["", f"Р”РѕРї. РіСЂСѓРїРїС‹ РёР· РєРѕРјРјРµРЅС‚Р°СЂРёРµРІ: {nums}"]
        for raw in w.get('extra_groups_raw', []):
            lines.append(f"---\n{raw[:300]}")

    text = '\n'.join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_debug_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """РџРѕРєР°Р·С‹РІР°РµС‚ СЂР°СЃРїР°СЂСЃРµРЅРЅС‹Рµ РґР°РЅРЅС‹Рµ РїРѕСЃР»РµРґРЅРµРіРѕ Long Run (С‚РѕР»СЊРєРѕ РґР»СЏ Р°РґРјРёРЅРѕРІ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("РќРµС‚ РґРѕСЃС‚СѓРїР°.")
        return
    if not last_long_run:
        await update.message.reply_text("Long Run РµС‰С‘ РЅРµ Р·Р°РіСЂСѓР¶Р°Р»СЃСЏ. РЎРЅР°С‡Р°Р»Р° /long.")
        return

    w = last_long_run
    lines = [
        f"рџ“… {w.get('weekday', '').capitalize()} {w.get('workout_date', '')}",
        f"РўРёРї: {w.get('workout_type', 'вЂ”')}",
        f"рџ“Ќ {w.get('location', 'вЂ”')}",
        f"вЏ° {w.get('schedule', 'вЂ”')}",
        f"рџ“Џ РћР±СЉС‘Рј: {w.get('total_volume_km', 'вЂ”')}",
        f"even_pace_available: {w.get('even_pace_available', False)}",
        f"is_past: {w.get('is_past', False)}",
        "",
        "Р“Р РЈРџРџР« (СЂР°СЃРїР°СЂСЃРµРЅРЅС‹Рµ):",
    ]
    for g in (w.get("groups") or []):
        label = g.get("label") or f"Р“СЂСѓРїРїР° {g.get('number', '?')}"
        pace_start = g.get("pace_start", "вЂ”")
        pace_end = g.get("pace_end", "вЂ”")
        prog = "РїСЂРѕРіСЂРµСЃСЃРёСЏ" if g.get("progression") else "СЂРѕРІРЅС‹Р№"
        lines.append(f"  {label}: {pace_start} в†’ {pace_end} ({prog})")

    lines += ["", "RAW РўР•РљРЎРў (РїРµСЂРІС‹Рµ 2000 СЃРёРјРІ.):", (w.get("groups_raw") or "")[:2000]]

    text = '\n'.join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_ratings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """РџРѕСЃР»РµРґРЅРёРµ 20 РѕС†РµРЅРѕРє СЂРµРєРѕРјРµРЅРґР°С†РёР№ (С‚РѕР»СЊРєРѕ РґР»СЏ Р°РґРјРёРЅРѕРІ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("РќРµС‚ РґРѕСЃС‚СѓРїР°.")
        return
    rows = get_recent_ratings(20)
    if not rows:
        await update.message.reply_text("РћС†РµРЅРѕРє РїРѕРєР° РЅРµС‚.")
        return
    lines = ["в­ђ РџРѕСЃР»РµРґРЅРёРµ РѕС†РµРЅРѕРє (РґРѕ 20):\n"]
    for r in rows:
        rating, ai_mode_, comment, created_at, workout_date, name, username = (
            r[1], r[2], r[3], r[4], r[5], r[6], r[7]
        )
        date_fmt = (created_at or "")[:10]
        uname = f" (@{username})" if username else ""
        stars = rating * "в­ђ" if rating >= 8 else (rating * "рџџЎ" if rating >= 5 else rating * "рџ”ґ")
        comment_str = f"\n   рџ’¬ {comment}" if comment else ""
        lines.append(f"{rating}/10 вЂ” {name}{uname} [{workout_date}] {date_fmt} [{ai_mode_}]{comment_str}")
    text = "\n".join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_feedbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """РџРѕСЃР»РµРґРЅРёРµ 20 СЃРѕРѕР±С‰РµРЅРёР№ РѕР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё (С‚РѕР»СЊРєРѕ РґР»СЏ Р°РґРјРёРЅРѕРІ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("РќРµС‚ РґРѕСЃС‚СѓРїР°.")
        return
    rows = get_recent_feedbacks(20)
    if not rows:
        await update.message.reply_text("РћР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё РїРѕРєР° РЅРµС‚.")
        return
    lines = ["рџ’¬ РџРѕСЃР»РµРґРЅРёРµ СЃРѕРѕР±С‰РµРЅРёСЏ (РґРѕ 20):\n"]
    for r in rows:
        fb_type, fb_text, created_at, name, username = r[1], r[2], r[3], r[4], r[5]
        date_fmt = (created_at or "")[:10]
        uname = f" (@{username})" if username else ""
        type_emoji = "рџђ›" if fb_type == "bug" else "рџ’Ў"
        lines.append(f"{type_emoji} {name}{uname} [{date_fmt}]\n{fb_text[:300]}")
    text = "\n\n".join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


# в”Ђв”Ђ Р”Р’РЈРҐРЁРђР“РћР’РђРЇ РћР‘Р РђР‘РћРўРљРђ (Р°РЅР°Р»РёР· С‚СЂРµРЅРёСЂРѕРІРѕРє) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

def _format_analysis_result(result: dict, mode: str) -> str:
    """РљСЂР°СЃРёРІРѕ С„РѕСЂРјР°С‚РёСЂСѓРµС‚ СЂРµР·СѓР»СЊС‚Р°С‚ analyze_workout РґР»СЏ Р°РґРјРёРЅР°."""
    stats = result.get("_stats", {})
    time_sec = stats.get("time_sec", "?")
    lines = [f"рџ”¬ РђРЅР°Р»РёР· С‚СЂРµРЅРёСЂРѕРІРєРё (СЂРµР¶РёРј {mode}, {time_sec}СЃ)\n"]

    if result.get("is_valid"):
        lines.append("вњ… Р’Р°Р»РёРґРЅС‹Р№ Р°РЅРѕРЅСЃ")
    else:
        lines.append(f"вќЊ РќРµ Р°РЅРѕРЅСЃ: {result.get('reject_reason') or 'вЂ”'}")

    wtype = result.get("workout_type", "вЂ”")
    lines.append(f"РўРёРї: {wtype} | Р”Р°С‚Р°: {result.get('workout_date', 'вЂ”')}")
    lines.append(f"\nрџ“‹ РЎСѓС‚СЊ: {result.get('summary', 'вЂ”')}")

    structure = result.get("structure") or []
    groups = result.get("groups") or []

    if wtype == "interval" and structure:
        # РќРѕРІР°СЏ СЃС…РµРјР°: СЃС‚СЂСѓРєС‚СѓСЂР° РѕРґРёРЅ СЂР°Р· + РіСЂСѓРїРїС‹ РєР°Рє С‚РµРјРїС‹ РїРѕ Р±Р»РѕРєР°Рј
        lines.append("\nрџЏ— РЎС‚СЂСѓРєС‚СѓСЂР°:")
        for b in structure:
            blk = b.get("block", "?")
            if b.get("type") == "easy":
                desc = b.get("description") or "Р»С‘РіРєРёР№ Р±РµРі"
                lines.append(f"  Р‘Р»РѕРє {blk}: {b.get('distance_m', '?')}Рј вЂ” {desc}")
            else:
                purpose = f" вЂ” {b['purpose']}" if b.get("purpose") else ""
                lines.append(
                    f"  Р‘Р»РѕРє {blk}: {b.get('reps', '?')}Г—{b.get('work_distance_m', '?')}Рј"
                    f" / {b.get('recovery_distance_m', '?')}Рј РІРѕСЃСЃС‚{purpose}"
                )
        if result.get("overall_purpose"):
            lines.append(f"рџЋЇ Р¦РµР»СЊ С‚СЂРµРЅРёСЂРѕРІРєРё: {result['overall_purpose']}")
        if result.get("block_contrast"):
            lines.append(f"рџ”Ђ РљРѕРЅС‚СЂР°СЃС‚ Р±Р»РѕРєРѕРІ: {result['block_contrast']}")
        if result.get("target_athlete"):
            lines.append(f"рџЏѓ Р”Р»СЏ РєРѕРіРѕ: {result['target_athlete']}")
        if result.get("intensity_level"):
            lines.append(f"рџ”Ґ РўСЏР¶РµСЃС‚СЊ: {result['intensity_level']}")
        if result.get("what_to_watch"):
            lines.append(f"рџ‘Ђ РќР° С‡С‚Рѕ РѕР±СЂР°С‚РёС‚СЊ РІРЅРёРјР°РЅРёРµ: {result['what_to_watch']}")
        if result.get("total_volume_km") is not None:
            lines.append(f"рџ“Џ РћР±СЉС‘Рј: {result['total_volume_km']} РєРј")
        if result.get("is_borderline"):
            note = result.get("borderline_note")
            lines.append(f"вљ–пёЏ РџРѕРіСЂР°РЅРёС‡РЅР°СЏ: РґР°{f' вЂ” {note}' if note else ''}")

        lines.append(f"\nР“СЂСѓРїРїС‹ ({len(groups)}):")
        for g in groups:
            num = g.get("number", "?")
            tags = []
            if g.get("from_comment"):
                tags.append("рџ’¬РёР· РєРѕРјРј.")
            if g.get("reps_override"):
                tags.append(f"РїРѕРІС‚РѕСЂРѕРІ: {g['reps_override']}")
            if g.get("track_note"):
                tags.append(str(g["track_note"]))
            tag_str = f" ({'; '.join(tags)})" if tags else ""

            if g.get("health_group"):
                lines.append(f"  {num}{tag_str}: Р±РµРі/С…РѕРґСЊР±Р° С‡РµСЂРµРґРѕРІР°РЅРёРµ (РґР»СЏ РЅР°С‡РёРЅР°СЋС‰РёС…)")
                continue

            block_strs = []
            for bl in (g.get("blocks") or []):
                ar = "рџџў" if bl.get("active_recovery") else "вљЄ"
                rp = bl.get("recovery_pace") or "вЂ”"
                block_strs.append(
                    f"Р±Р»{bl.get('block', '?')} {bl.get('work_pace', 'вЂ”')}/РєРј (РІРѕСЃСЃС‚ {rp} {ar})"
                )
            body = "; ".join(block_strs) if block_strs else "вЂ”"
            lines.append(f"  {num}{tag_str}: {body}")
    else:
        # Long РёР»Рё СЃС‚Р°СЂС‹Р№ С„РѕСЂРјР°С‚: РіСЂСѓРїРїС‹ СЃ С‚РµРєСЃС‚РѕРІС‹Рј work
        lines.append(f"\nР“СЂСѓРїРїС‹ ({len(groups)}):")
        for g in groups:
            recovery = g.get("recovery")
            line = f"  {g.get('number', '?')}. {g.get('work', 'вЂ”')}"
            if recovery and str(recovery).lower() != "none":
                ar = " рџџўР°РєС‚РёРІ.РІРѕСЃСЃС‚." if g.get("active_recovery") else ""
                rec_pace = g.get("recovery_pace") or "вЂ”"
                line += f"\n     в†» РІРѕСЃСЃС‚: {recovery} ({rec_pace}){ar}"
            lines.append(line)

    extra = result.get("extra_groups") or []
    if extra:
        lines.append(f"\nР”РѕРї. РіСЂСѓРїРїС‹ ({len(extra)}):")
        for e in extra:
            lines.append(f"  {e.get('number', '?')}: {e.get('description', 'вЂ”')} [{e.get('source', 'вЂ”')}]")
    else:
        lines.append("\nР”РѕРї. РіСЂСѓРїРїС‹: РЅРµС‚")

    if wtype == "long":
        prog = "РґР°" if result.get("has_progression") else "РЅРµС‚"
        even = "РґР°" if result.get("even_pace_available") else "РЅРµС‚"
        lines.append(f"\nРџСЂРѕРіСЂРµСЃСЃРёСЏ: {prog} | Р РѕРІРЅС‹Р№ С‚РµРјРї: {even}")

    lines.append(f"\nрџ“ќ Р—Р°РјРµС‚РєРё С‚СЂРµРЅРµСЂР°: {result.get('coach_notes') or 'вЂ”'}")
    lines.append(f"рџ—‘ РџСЂРѕРёРіРЅРѕСЂРёСЂРѕРІР°РЅРѕ: {result.get('ignored') or 'вЂ”'}")
    return "\n".join(lines)


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Р’С‹Р±РѕСЂ С‚РёРїР° С‚СЂРµРЅРёСЂРѕРІРєРё РґР»СЏ Р°РЅР°Р»РёР·Р° С‡РµСЂРµР· DeepSeek (С‚РѕР»СЊРєРѕ РґР»СЏ Р°РґРјРёРЅРѕРІ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("РќРµС‚ РґРѕСЃС‚СѓРїР°.")
        return
    await update.message.reply_text(
        "РљР°РєСѓСЋ С‚СЂРµРЅРёСЂРѕРІРєСѓ РїСЂРѕР°РЅР°Р»РёР·РёСЂРѕРІР°С‚СЊ?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("вљЎ РРЅС‚РµСЂРІР°Р»СЊРЅР°СЏ (РІС‚/РїС‚)", callback_data="analyze_interval"),
            InlineKeyboardButton("рџ•ђ Long Run (РІСЃ)",        callback_data="analyze_long"),
        ]])
    )


async def _run_analyze_and_show(workout: dict, query, context: ContextTypes.DEFAULT_TYPE):
    """РђРЅР°Р»РёР·РёСЂСѓРµС‚ РЅР°Р№РґРµРЅРЅС‹Р№ РїРѕСЃС‚ С‚СЂРµРЅРёСЂРѕРІРєРё Рё РїРѕРєР°Р·С‹РІР°РµС‚ СЂРµР·СѓР»СЊС‚Р°С‚ Р°РґРјРёРЅСѓ."""
    raw_text = workout.get("raw_text", "")
    comments_text = workout.get("comments_text", "")
    post_id = workout.get("post_id")

    if not raw_text:
        await query.edit_message_text("вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕР»СѓС‡РёС‚СЊ С‚РµРєСЃС‚ РїРѕСЃС‚Р° РґР»СЏ Р°РЅР°Р»РёР·Р°.")
        return

    mode = get_preprocess_mode()
    await query.edit_message_text(
        f"вЏі РђРЅР°Р»РёР·РёСЂСѓСЋ С‡РµСЂРµР· DeepSeek (СЂРµР¶РёРј {mode})...\nРњРѕР¶РµС‚ Р·Р°РЅСЏС‚СЊ 1-2 РјРёРЅСѓС‚С‹."
    )

    import functools
    result = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(analyze_workout, raw_text, comments_text, mode)
    )

    if not result:
        await query.edit_message_text("вќЊ РђРЅР°Р»РёР· РЅРµ СѓРґР°Р»СЃСЏ (РїСѓСЃС‚РѕР№ РѕС‚РІРµС‚ РјРѕРґРµР»Рё). РџРѕРїСЂРѕР±СѓР№ РµС‰С‘ СЂР°Р·.")
        return

    # РЎРѕС…СЂР°РЅСЏРµРј СЂРµР·СѓР»СЊС‚Р°С‚ РІ Р‘Р”
    try:
        import json as _json
        save_workout_analysis(
            post_id=post_id,
            workout_date=result.get("workout_date", ""),
            workout_type=result.get("workout_type", ""),
            is_valid=1 if result.get("is_valid") else 0,
            raw_text=raw_text,
            analyzed_json=_json.dumps(result, ensure_ascii=False),
            analysis_mode=mode,
        )
    except Exception as e:
        logger.error(f"save_workout_analysis error: {e}")

    text = _format_analysis_result(result, mode)
    first = True
    for i in range(0, len(text), 4096):
        chunk = text[i:i + 4096]
        if first:
            await query.edit_message_text(chunk)
            first = False
        else:
            await context.bot.send_message(query.from_user.id, chunk)


def _build_preprocess_text(current: str) -> str:
    label = "рџ§  deep (deepseek-v4-pro)" if current == "deep" else "вљЎ smart (deepseek-v4-flash)"
    return (
        "рџ”¬ Р РµР¶РёРј Р°РЅР°Р»РёР·Р° С‚СЂРµРЅРёСЂРѕРІРѕРє (preprocess)\n\n"
        f"РўРµРєСѓС‰РёР№: {label}\n\n"
        "рџ§  deep вЂ” РјРµРґР»РµРЅРЅРµРµ, РјР°РєСЃРёРјР°Р»СЊРЅРѕРµ РєР°С‡РµСЃС‚РІРѕ\n"
        "вљЎ smart вЂ” Р±С‹СЃС‚СЂРµРµ, С‡СѓС‚СЊ РїСЂРѕС‰Рµ\n\n"
        "Р’С‹Р±РµСЂРё СЂРµР¶РёРј:"
    )


def _build_preprocess_keyboard(current: str) -> InlineKeyboardMarkup:
    def btn(key, label):
        mark = "вњ“ " if key == current else ""
        return InlineKeyboardButton(f"{mark}{label}", callback_data=f"preprocess_set_{key}")
    return InlineKeyboardMarkup([
        [btn("deep", "рџ§  deep"), btn("smart", "вљЎ smart")],
    ])


async def cmd_preprocess_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """РџРµСЂРµРєР»СЋС‡Р°С‚РµР»СЊ СЂРµР¶РёРјР° Р°РЅР°Р»РёР·Р° С‚СЂРµРЅРёСЂРѕРІРѕРє deep/smart (С‚РѕР»СЊРєРѕ РґР»СЏ Р°РґРјРёРЅРѕРІ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("РќРµС‚ РґРѕСЃС‚СѓРїР°.")
        return
    current = get_preprocess_mode()
    await update.message.reply_text(
        _build_preprocess_text(current),
        reply_markup=_build_preprocess_keyboard(current),
    )


# в”Ђв”Ђ РўР•РЎРў РЁРђР“Рђ 2 (СЂРµРєРѕРјРµРЅРґР°С†РёСЏ РЅР° РіРѕС‚РѕРІРѕРј Р°РЅР°Р»РёР·Рµ) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

async def _collect_admin_user_data(db_user_id: int) -> tuple[dict, list[str]]:
    """РЎРѕР±РёСЂР°РµС‚ user_data С‚РµРєСѓС‰РµРіРѕ Р°РґРјРёРЅР° РґР»СЏ recommend_*. Р’РѕР·РІСЂР°С‰Р°РµС‚ (user_data, missing)."""
    missing = []
    profile = get_user_profile(db_user_id)
    spec = (profile or {}).get("specialization")

    zinfo = zones.get_pace_zones(db_user_id)
    if not zinfo or not zinfo.get("zones"):
        missing.append("РїРµСЂСЃРѕРЅР°Р»СЊРЅС‹Рµ Р·РѕРЅС‹ (РЅРµС‚ VO2max/Р›Рџ РІ РїСЂРѕС„РёР»Рµ)")

    recovery = None
    try:
        recovery = await _get_recovery_data(db_user_id, force_fresh=True)
    except Exception as e:
        logger.warning(f"test: recovery error for {db_user_id}: {e}")
    if not recovery:
        missing.append("РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёРµ (Whoop/Garmin/COROS/Polar) вЂ” РІР·СЏС‚Рѕ РЅРµР№С‚СЂР°Р»СЊРЅРѕРµ 70")

    user_data = {"db_user_id": db_user_id, "specialization": spec, "recovery": recovery}
    return user_data, missing


async def _run_test_step2(update, context, *, long: bool):
    """РћР±С‰Р°СЏ Р»РѕРіРёРєР° /test_workout Рё /test_long."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("РќРµС‚ РґРѕСЃС‚СѓРїР°.")
        return

    user = update.effective_user
    db_user_id = get_or_create_user(user.id, user.full_name, user.username)
    label = "Long Run" if long else "РёРЅС‚РµСЂРІР°Р»СЊРЅСѓСЋ"
    msg = await update.message.reply_text(f"рџ§Є РС‰Сѓ {label} С‚СЂРµРЅРёСЂРѕРІРєСѓ РІ РєР°РЅР°Р»Рµ...")

    workout = await (find_next_long_run() if long else find_next_workout(only_interval=True))
    if not workout:
        await msg.edit_text("рџ” РќРµ РЅР°С€С‘Р» РїРѕРґС…РѕРґСЏС‰СѓСЋ С‚СЂРµРЅРёСЂРѕРІРєСѓ РІ РєР°РЅР°Р»Рµ.")
        return

    mode = get_preprocess_mode()
    await msg.edit_text(f"рџ§Є РђРЅР°Р»РёР·РёСЂСѓСЋ С‡РµСЂРµР· DeepSeek (СЂРµР¶РёРј {mode})...\nРњРѕР¶РµС‚ Р·Р°РЅСЏС‚СЊ 1-2 РјРёРЅСѓС‚С‹.")

    import functools
    analysis = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(analyze_workout, workout["raw_text"], workout["comments_text"], mode)
    )
    if not analysis:
        await msg.edit_text("вќЊ РђРЅР°Р»РёР· РЅРµ СѓРґР°Р»СЃСЏ (РїСѓСЃС‚РѕР№ РѕС‚РІРµС‚ РјРѕРґРµР»Рё).")
        return

    user_data, missing = await _collect_admin_user_data(db_user_id)

    if long:
        rec = claude_advisor.recommend_long(analysis, user_data)
    else:
        rec = claude_advisor.recommend_group(analysis, user_data)

    # в”Ђв”Ђ Р—Р°РіРѕР»РѕРІРѕРє С‚РµСЃС‚Р° СЃ РїРѕРјРµС‚РєРѕР№ С‡РµРіРѕ РЅРµ С…РІР°С‚РёР»Рѕ в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    header = [
        f"рџ§Є <b>РўРµСЃС‚ РЁР°РіР° 2 вЂ” {'Long Run' if long else 'РРЅС‚РµСЂРІР°Р»СЊРЅР°СЏ'}</b>",
        f"РђРЅР°Р»РёР·: {analysis.get('workout_date', 'вЂ”')} В· "
        f"valid={analysis.get('is_valid')} В· СЂРµР¶РёРј {mode}",
    ]
    if not long:
        header.append(f"is_borderline: {analysis.get('is_borderline')}")
    if missing:
        header.append("вљ пёЏ РќРµ С…РІР°С‚РёР»Рѕ РґР°РЅРЅС‹С…: " + "; ".join(missing))
    else:
        src = (rec or {}).get("zones_source")
        header.append(f"вњ… Р”Р°РЅРЅС‹Рµ РїРѕР»РЅС‹Рµ (Р·РѕРЅС‹: {src})")

    await msg.edit_text("\n".join(header), parse_mode="HTML")

    # в”Ђв”Ђ РЎР°Рј РІС‹РІРѕРґ СЂРµРєРѕРјРµРЅРґР°С†РёРё (РєР°Рє СѓРІРёРґРёС‚ РїРѕР»СЊР·РѕРІР°С‚РµР»СЊ) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    if not rec or not rec.get("ok"):
        note = (rec or {}).get("note", "СЂРµРєРѕРјРµРЅРґР°С†РёСЏ РЅРµРґРѕСЃС‚СѓРїРЅР°")
        await context.bot.send_message(user.id, f"вќЊ {note}")
        return

    rec_text = rec.get("text", "(РїСѓСЃС‚РѕР№ РІС‹РІРѕРґ)")
    for i in range(0, len(rec_text), 4096):
        await context.bot.send_message(user.id, rec_text[i:i + 4096])


async def cmd_test_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """РўРµСЃС‚ РЁР°РіР° 2 РґР»СЏ РёРЅС‚РµСЂРІР°Р»СЊРЅРѕР№: Р°РЅР°Р»РёР· + recommend_group РЅР° РґР°РЅРЅС‹С… Р°РґРјРёРЅР° (Р°РґРјРёРЅ)."""
    await _run_test_step2(update, context, long=False)


async def cmd_test_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """РўРµСЃС‚ РЁР°РіР° 2 РґР»СЏ РґР»РёС‚РµР»СЊРЅРѕР№: Р°РЅР°Р»РёР· + recommend_long РЅР° РґР°РЅРЅС‹С… Р°РґРјРёРЅР° (Р°РґРјРёРЅ)."""
    await _run_test_step2(update, context, long=True)


async def _reanalyze_one(workout: dict, mode: str) -> str:
    """РџСЂРёРЅСѓРґРёС‚РµР»СЊРЅС‹Р№ РїРµСЂРµР°РЅР°Р»РёР· РѕРґРЅРѕРіРѕ Р°РЅРѕРЅСЃР° (РёРіРЅРѕСЂ idempotency) в†’ Р·Р°РїРёСЃСЊ РІ workout_analysis.
    Р’РѕР·РІСЂР°С‰Р°РµС‚ СЃС‚СЂРѕРєСѓ СЃС‚Р°С‚СѓСЃР° РґР»СЏ Р°РґРјРёРЅР°.
    """
    import json as _json, functools
    label = "рџ•ђ Long Run" if workout.get("workout_type") == "long" else "вљЎ РРЅС‚РµСЂРІР°Р»СЊРЅР°СЏ"
    date_fmt = workout.get("workout_date", "вЂ”")
    raw_text = workout.get("raw_text") or ""
    comments_text = workout.get("comments_text") or ""
    edit_date = workout.get("edit_date")
    extra = workout.get("extra_groups") or []

    result = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(analyze_workout, raw_text, comments_text, mode)
    )
    if not result:
        return f"{label} вЂ” {date_fmt}: вќЊ Р°РЅР°Р»РёР· РЅРµ СѓРґР°Р»СЃСЏ (РїСѓСЃС‚РѕР№ РѕС‚РІРµС‚ РјРѕРґРµР»Рё)"

    save_workout_analysis(
        post_id=workout.get("post_id"),
        workout_date=result.get("workout_date", ""),
        workout_type=result.get("workout_type", ""),
        is_valid=1 if result.get("is_valid") else 0,
        raw_text=raw_text,
        analyzed_json=_json.dumps(result, ensure_ascii=False),
        analysis_mode=mode,
        extra_groups_json=_json.dumps(extra, ensure_ascii=False),
        edit_date=edit_date,
    )
    n_groups = len(result.get("groups") or [])
    n_extra = len(result.get("extra_groups") or [])
    logger.info(f"reanalyze: post_id={workout.get('post_id')} РѕР±РЅРѕРІР»С‘РЅ РІСЂСѓС‡РЅСѓСЋ "
                f"(type={result.get('workout_type')}, valid={result.get('is_valid')})")
    return (
        f"{label} вЂ” {result.get('workout_date', date_fmt)} | СЂРµР¶РёРј {mode} | "
        f"valid={result.get('is_valid')}\n"
        f"   РіСЂСѓРїРї: {n_groups}, РґРѕРї. РіСЂСѓРїРї: {n_extra} вЂ” РѕР±РЅРѕРІР»РµРЅРѕ"
    )


async def cmd_reanalyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Р СѓС‡РЅРѕР№ С„РѕСЂСЃ РїРµСЂРµР°РЅР°Р»РёР·Р° СЃРІРµР¶РёС… Р°РЅРѕРЅСЃРѕРІ (interval + long) в†’ РєСЌС€ workout_analysis (Р°РґРјРёРЅ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("РќРµС‚ РґРѕСЃС‚СѓРїР°.")
        return

    mode = get_preprocess_mode()
    msg = await update.message.reply_text(
        f"рџ”Ѓ РџСЂРёРЅСѓРґРёС‚РµР»СЊРЅС‹Р№ РїРµСЂРµР°РЅР°Р»РёР· СЃРІРµР¶РёС… Р°РЅРѕРЅСЃРѕРІ (СЂРµР¶РёРј {mode})...\nРњРѕР¶РµС‚ Р·Р°РЅСЏС‚СЊ 1-2 РјРёРЅСѓС‚С‹ РЅР° РєР°Р¶РґС‹Р№."
    )

    lines = [f"рџ”Ѓ <b>РџРµСЂРµР°РЅР°Р»РёР· РІС‹РїРѕР»РЅРµРЅ (СЂРµР¶РёРј {mode})</b>\n"]

    workout = await find_next_workout(only_interval=True)
    if workout and workout.get("post_id"):
        lines.append(await _reanalyze_one(workout, mode))
    else:
        lines.append("вљЎ РРЅС‚РµСЂРІР°Р»СЊРЅР°СЏ вЂ” Р°РЅРѕРЅСЃ РЅРµ РЅР°Р№РґРµРЅ")

    workout_lr = await find_next_long_run()
    if workout_lr and workout_lr.get("post_id"):
        lines.append(await _reanalyze_one(workout_lr, mode))
    else:
        lines.append("рџ•ђ Long Run вЂ” Р°РЅРѕРЅСЃ РЅРµ РЅР°Р№РґРµРЅ")

    await msg.edit_text("\n".join(lines), parse_mode="HTML")


# в”Ђв”Ђ РљРќРћРџРљР в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    user = update.effective_user
    _mark_user_active_if_needed(user.id, user.full_name, user.username)

    if query.data == "main_menu":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        await _show_main_menu(query, user, db_user_id)

    elif query.data == "main_menu_new":
        # РџСЂРёС…РѕРґРёС‚ РёР· РєРЅРѕРїРѕРє РЅР° СЂРµРєРѕРјРµРЅРґР°С†РёРё вЂ” РѕС‚РїСЂР°РІР»СЏРµРј РќРћР’Р«Рњ СЃРѕРѕР±С‰РµРЅРёРµРј,
        # С‡С‚РѕР±С‹ РЅРµ РїРµСЂРµР·Р°РїРёСЃС‹РІР°С‚СЊ С‚РµРєСЃС‚ СЂРµРєРѕРјРµРЅРґР°С†РёРё.
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        text, keyboard = _build_main_menu_content(user, db_user_id)
        await context.bot.send_message(user.id, text, reply_markup=keyboard)

    elif query.data == "show_settings":
        # РўРѕР»СЊРєРѕ РєРЅРѕРїРєРё РјРµРЅСЏРµРј, С‚РµРєСЃС‚ РѕСЃС‚Р°РІР»СЏРµРј
        await query.edit_message_reply_markup(reply_markup=_build_screen2_keyboard())

    elif query.data == "settings_menu":
        # Р’РѕР·РІСЂР°С‚ РІ Р­РєСЂР°РЅ 2 РёР· РІР»РѕР¶РµРЅРЅС‹С… СЂР°Р·РґРµР»РѕРІ (РїСЂРѕС„РёР»СЊ / СѓРІРµРґРѕРјР»РµРЅРёСЏ / СЃРµСЂРІРёСЃС‹)
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        text, _ = _build_main_menu_content(user, db_user_id)
        await query.edit_message_text(text, reply_markup=_build_screen2_keyboard())

    elif query.data == "show_services":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        # РўРѕР»СЊРєРѕ РєРЅРѕРїРєРё РјРµРЅСЏРµРј, С‚РµРєСЃС‚ РѕСЃС‚Р°РІР»СЏРµРј
        await query.edit_message_reply_markup(
            reply_markup=_build_screen3_keyboard(db_user_id)
        )

    elif query.data == "connect_strava":
        auth_url = get_auth_url(user.id)
        keyboard = [
            [InlineKeyboardButton("рџ”— Р’РѕР№С‚Рё РІ Strava", url=auth_url)],
            [InlineKeyboardButton("в†ђ РќР°Р·Р°Рґ", callback_data="show_services")],
        ]
        await query.edit_message_text(
            "вљ пёЏ Strava РІСЂРµРјРµРЅРЅРѕ РѕРіСЂР°РЅРёС‡РµРЅР° вЂ” РїРѕРґРєР»СЋС‡РµРЅРёРµ РЅРѕРІС‹С… РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№ РЅР° РїСЂРѕРІРµСЂРєРµ Сѓ Strava.\n"
            "РСЃРїРѕР»СЊР·СѓР№ Garmin РёР»Рё COROS (/connect_garmin, /connect_coros) РґР»СЏ РїРѕР»РЅРѕС†РµРЅРЅРѕР№ СЂР°Р±РѕС‚С‹.\n\n"
            "РќР°Р¶РјРё РєРЅРѕРїРєСѓ Рё Р°РІС‚РѕСЂРёР·СѓР№СЃСЏ РІ Strava.\n\n"
            "РџРѕСЃР»Рµ Р°РІС‚РѕСЂРёР·Р°С†РёРё С‚С‹ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё РїРѕР»СѓС‡РёС€СЊ СЃРѕРѕР±С‰РµРЅРёРµ РІ Telegram вЂ” РЅРёС‡РµРіРѕ РєРѕРїРёСЂРѕРІР°С‚СЊ РЅРµ РЅСѓР¶РЅРѕ.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "connect_whoop_btn":
        from whoop import get_auth_url as whoop_auth_url
        auth_url = whoop_auth_url(user.id)
        keyboard = [
            [InlineKeyboardButton("рџ”— Р’РѕР№С‚Рё РІ Whoop", url=auth_url)],
            [InlineKeyboardButton("в†ђ РќР°Р·Р°Рґ", callback_data="show_services")],
        ]
        context.user_data["awaiting_whoop_code"] = True
        await query.edit_message_text(
            "РќР°Р¶РјРё РєРЅРѕРїРєСѓ Рё Р°РІС‚РѕСЂРёР·СѓР№СЃСЏ РІ Whoop.\n\n"
            "РџРѕСЃР»Рµ Р°РІС‚РѕСЂРёР·Р°С†РёРё Р±СЂР°СѓР·РµСЂ РѕС‚РєСЂРѕРµС‚ СЃС‚СЂР°РЅРёС†Сѓ СЃ JSON вЂ” "
            "СЃРєРѕРїРёСЂСѓР№ РІРµСЃСЊ URL РёР· Р°РґСЂРµСЃРЅРѕР№ СЃС‚СЂРѕРєРё Рё РѕС‚РїСЂР°РІСЊ РјРЅРµ СЃСЋРґР°.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "connect_garmin_btn":
        context.user_data["awaiting_garmin"] = "email"
        await query.edit_message_text(
            "РџРѕРґРєР»СЋС‡РµРЅРёРµ Garmin Connect\n\n"
            "Email Рё РїР°СЂРѕР»СЊ С…СЂР°РЅСЏС‚СЃСЏ РІ Р·Р°С€РёС„СЂРѕРІР°РЅРЅРѕРј РІРёРґРµ (AES-256).\n\n"
            "Р’РІРµРґРё email РѕС‚ Garmin Connect:"
        )

    elif query.data == "connect_coros_btn":
        context.user_data["awaiting_coros"] = "email"
        await query.edit_message_text(
            "РџРѕРґРєР»СЋС‡РµРЅРёРµ COROS\n\n"
            "Email Рё РїР°СЂРѕР»СЊ С…СЂР°РЅСЏС‚СЃСЏ РІ Р·Р°С€РёС„СЂРѕРІР°РЅРЅРѕРј РІРёРґРµ (AES-256).\n\n"
            "Р’РІРµРґРё email РѕС‚ Р°РєРєР°СѓРЅС‚Р° COROS:"
        )

    elif query.data == "connect_polar_btn":
        from polar import get_auth_url as polar_auth_url
        auth_url = polar_auth_url(user.id)
        if not auth_url:
            await query.edit_message_text(
                "вќЊ Polar РЅРµ РЅР°СЃС‚СЂРѕРµРЅ. РћР±СЂР°С‚РёС‚РµСЃСЊ Рє Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂСѓ.",
                reply_markup=_build_screen3_keyboard(get_or_create_user(user.id, user.full_name, user.username))
            )
            return
        keyboard = [
            [InlineKeyboardButton("рџ”— Р’РѕР№С‚Рё РІ Polar", url=auth_url)],
            [InlineKeyboardButton("в†ђ РќР°Р·Р°Рґ", callback_data="show_services")],
        ]
        await query.edit_message_text(
            "РџРѕРґРєР»СЋС‡РµРЅРёРµ Polar\n\n"
            "РќР°Р¶РјРё РєРЅРѕРїРєСѓ Рё Р°РІС‚РѕСЂРёР·СѓР№СЃСЏ РІ Polar Flow.\n\n"
            "РџРѕСЃР»Рµ Р°РІС‚РѕСЂРёР·Р°С†РёРё С‚С‹ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё РїРѕР»СѓС‡РёС€СЊ СЃРѕРѕР±С‰РµРЅРёРµ РІ Telegram вЂ” РЅРёС‡РµРіРѕ РєРѕРїРёСЂРѕРІР°С‚СЊ РЅРµ РЅСѓР¶РЅРѕ.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "svc_noop":
        pass  # РЅР°Р¶Р°С‚РёРµ РЅР° Р»РµР№Р±Р» РїРѕРґРєР»СЋС‡С‘РЅРЅРѕРіРѕ СЃРµСЂРІРёСЃР° вЂ” РЅРёС‡РµРіРѕ РЅРµ РґРµР»Р°РµРј

    elif query.data == "svc_cancel":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        await query.edit_message_text(
            "рџ”— РџРѕРґРєР»СЋС‡С‘РЅРЅС‹Рµ СЃРµСЂРІРёСЃС‹",
            reply_markup=_build_screen3_keyboard(db_user_id)
        )

    elif query.data.startswith("disc_ask_"):
        svc = query.data[len("disc_ask_"):]
        name = _svc_name(svc)
        await query.edit_message_text(
            f"РћС‚РєР»СЋС‡РёС‚СЊ {name}?\n\nР”Р°РЅРЅС‹Рµ Р±СѓРґСѓС‚ СѓРґР°Р»РµРЅС‹ РёР· Р±РѕС‚Р°.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("вњ… Р”Р°, РѕС‚РєР»СЋС‡РёС‚СЊ", callback_data=f"disc_yes_{svc}"),
                InlineKeyboardButton("вќЊ РћС‚РјРµРЅР°",        callback_data="svc_cancel"),
            ]])
        )

    elif query.data.startswith("disc_yes_"):
        svc = query.data[len("disc_yes_"):]
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        delete_token(db_user_id, svc)
        done_msg = _svc_done_msg(svc)
        logger.info(f"РЎРµСЂРІРёСЃ {svc} РѕС‚РєР»СЋС‡С‘РЅ РґР»СЏ user {db_user_id}")
        await query.edit_message_text(
            f"вњ… {done_msg}.",
            reply_markup=_build_screen3_keyboard(db_user_id)
        )

    elif query.data == "get_morning":
        msg = await context.bot.send_message(user.id, "вЂпёЏ РџСЂРѕРІРµСЂСЏСЋ С‚РІРѕС‘ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёРµ...")
        await _send_morning_check(user.id, context, msg)

    elif query.data == "get_workout":
        msg = await context.bot.send_message(user.id, "рџ”Ќ РџРѕРґР±РёСЂР°СЋ С‚СЂРµРЅРёСЂРѕРІРєСѓ...")
        await _send_recommendation(user.id, user.full_name, context, long=False, msg=msg)

    elif query.data == "get_long_run":
        msg = await context.bot.send_message(user.id, "рџ”Ќ РџРѕРґР±РёСЂР°СЋ Long Run...")
        await _send_recommendation(user.id, user.full_name, context, long=True, msg=msg)

    elif query.data == "refresh_cache":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        await query.edit_message_text("вЏі РћР±РЅРѕРІР»СЏСЋ РґР°РЅРЅС‹Рµ РёР· РІСЃРµС… РїРѕРґРєР»СЋС‡С‘РЅРЅС‹С… СЃРµСЂРІРёСЃРѕРІ...")

        result_lines = ["вњ… Р”Р°РЅРЅС‹Рµ РѕР±РЅРѕРІР»РµРЅС‹!\n"]

        # Strava
        try:
            access_token = await ensure_valid_token(db_user_id)
            if access_token:
                athlete_data = await refresh_athlete_cache(db_user_id, access_token)
                if athlete_data:
                    load = athlete_data["training_load"]
                    result_lines.append(
                        f"рџџ  Strava: CTL {load.get('ctl', 'вЂ”')}, "
                        f"ATL {load.get('atl', 'вЂ”')}, TSB {load.get('tsb', 'вЂ”')}"
                    )
        except Exception as e:
            logger.error(f"Strava refresh error (button) for {user.id}: {e}")
            result_lines.append("рџџ  Strava: вќЊ РѕС€РёР±РєР°")

        # Garmin
        if get_token(db_user_id, "garmin"):
            try:
                from garmin import get_vo2max as _garmin_vo2max, get_training_readiness, get_lactate_threshold
                vo2max_val, readiness, lt = await asyncio.gather(
                    _garmin_vo2max(db_user_id),
                    get_training_readiness(db_user_id),
                    get_lactate_threshold(db_user_id),
                    return_exceptions=True,
                )
                profile = get_user_profile(db_user_id)
                garmin_parts = []
                if not isinstance(vo2max_val, Exception) and vo2max_val is not None:
                    if not (profile or {}).get("vo2max_locked"):
                        save_user_profile(db_user_id, vo2max=float(vo2max_val), vo2max_source="auto")
                    garmin_parts.append(f"VO2max {float(vo2max_val):.0f}")
                if not isinstance(lt, Exception) and lt:
                    if not (profile or {}).get("lactate_locked"):
                        save_user_profile(db_user_id,
                            lactate_threshold_pace=lt["pace"],
                            lactate_threshold_hr=lt.get("hr"),
                            lactate_source="auto")
                    garmin_parts.append(f"Р›Рџ {lt['pace']}")
                if not isinstance(readiness, Exception) and readiness and readiness.get("score") is not None:
                    garmin_parts.append(f"TR {readiness['score']}")
                result_lines.append(f"рџ”µ Garmin: {', '.join(garmin_parts) if garmin_parts else 'РѕР±РЅРѕРІР»РµРЅРѕ'}")
            except Exception as e:
                logger.error(f"Garmin refresh error (button) for {user.id}: {e}")
                result_lines.append("рџ”µ Garmin: вќЊ РѕС€РёР±РєР°")

        # COROS
        if get_token(db_user_id, "coros"):
            try:
                import coros as _coros
                coros_data = await _coros.get_full_data(db_user_id)
                load = (coros_data or {}).get("training_load") or {}
                coros_parts = []
                ctl = load.get("ctl")
                if ctl is not None:
                    coros_parts.append(f"CTL {ctl}")
                coros_vo2max = (coros_data or {}).get("fitness", {}).get("vo2max")
                if coros_vo2max is not None:
                    profile = get_user_profile(db_user_id)
                    if not (profile or {}).get("vo2max_locked"):
                        save_user_profile(db_user_id, vo2max=float(coros_vo2max), vo2max_source="auto")
                    coros_parts.append(f"VO2max {float(coros_vo2max):.0f}")
                result_lines.append(f"рџ”ґ COROS: {', '.join(coros_parts)}" if coros_parts else "рџ”ґ COROS: РѕР±РЅРѕРІР»РµРЅРѕ")
            except Exception as e:
                logger.error(f"COROS refresh error (button) for {user.id}: {e}")
                result_lines.append("рџ”ґ COROS: вќЊ РѕС€РёР±РєР°")

        # Polar
        if get_token(db_user_id, "polar"):
            try:
                import polar as _polar
                polar_data = await _polar.get_full_data(db_user_id)
                polar_vo2max = await _polar.get_vo2max(db_user_id)
                polar_parts = []
                if polar_vo2max is not None:
                    profile = get_user_profile(db_user_id)
                    if not (profile or {}).get("vo2max_locked"):
                        save_user_profile(db_user_id, vo2max=float(polar_vo2max), vo2max_source="auto")
                    polar_parts.append(f"VO2max {float(polar_vo2max):.0f}")
                result_lines.append(f"вќ„пёЏ Polar: {', '.join(polar_parts)}" if polar_parts else "вќ„пёЏ Polar: РѕР±РЅРѕРІР»РµРЅРѕ")
            except Exception as e:
                logger.error(f"Polar refresh error (button) for {user.id}: {e}")
                result_lines.append("вќ„пёЏ Polar: вќЊ РѕС€РёР±РєР°")

        if len(result_lines) == 1:
            result_lines.append("РќРµС‚ РїРѕРґРєР»СЋС‡С‘РЅРЅС‹С… СЃРµСЂРІРёСЃРѕРІ.\nРџРѕРґРєР»СЋС‡Рё С‚СЂРµРєРµСЂ РІ РќР°СЃС‚СЂРѕР№РєР°С… в†’ РЎРµСЂРІРёСЃС‹.")

        await query.edit_message_text("\n".join(result_lines), reply_markup=get_main_keyboard())

    elif query.data == "my_profile":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        profile = get_user_profile(db_user_id)
        await query.edit_message_text(
            _build_profile_text(profile),
            reply_markup=_build_profile_keyboard(profile)
        )

    elif query.data == "profile_set_vo2max":
        context.user_data["awaiting_profile"] = "set_vo2max"
        await query.edit_message_text(
            "Р’РІРµРґРё Р·РЅР°С‡РµРЅРёРµ VO2max (РјР»/РєРі/РјРёРЅ).\n\nРќР°РїСЂРёРјРµСЂ: 53"
        )

    elif query.data == "profile_set_lactate":
        context.user_data["awaiting_profile"] = "set_lactate_pace"
        await query.edit_message_text(
            "Р’РІРµРґРё С‚РµРјРї Р»Р°РєС‚Р°С‚РЅРѕРіРѕ РїРѕСЂРѕРіР° (РјРёРЅ:СЃРµРє РЅР° РєРј).\n\nРќР°РїСЂРёРјРµСЂ: 4:17"
        )

    elif query.data == "profile_set_gender":
        await query.edit_message_text(
            "Р’С‹Р±РµСЂРё РїРѕР»:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("рџ‘Ё РњСѓР¶СЃРєРѕР№", callback_data="profile_gender_male"),
                 InlineKeyboardButton("рџ‘© Р–РµРЅСЃРєРёР№", callback_data="profile_gender_female")],
            ])
        )

    elif query.data in ("profile_gender_male", "profile_gender_female"):
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        gender = "male" if query.data == "profile_gender_male" else "female"
        gender_label = "РњСѓР¶СЃРєРѕР№" if gender == "male" else "Р–РµРЅСЃРєРёР№"
        save_user_profile(db_user_id, gender=gender)
        profile = get_user_profile(db_user_id)
        await query.edit_message_text(
            f"вњ… РџРѕР» СЃРѕС…СЂР°РЅС‘РЅ: {gender_label}\n\n{_build_profile_text(profile)}",
            reply_markup=_build_profile_keyboard(profile)
        )

    elif query.data == "profile_set_specialization":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        current_spec = (get_user_profile(db_user_id) or {}).get("specialization")
        await query.edit_message_text(
            "Р’С‹Р±РµСЂРё СЃРїРµС†РёР°Р»РёР·Р°С†РёСЋ (РЅР° С‡С‚Рѕ РЅР°С†РµР»РµРЅС‹ С‚СЂРµРЅРёСЂРѕРІРєРё):",
            reply_markup=_build_specialization_keyboard(current_spec)
        )

    elif query.data.startswith("spec_set_"):
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        spec = query.data[len("spec_set_"):]
        if spec not in SPECIALIZATIONS:
            return
        save_user_profile(db_user_id, specialization=spec)
        profile = get_user_profile(db_user_id)
        await query.edit_message_text(
            f"вњ… РЎРїРµС†РёР°Р»РёР·Р°С†РёСЏ СЃРѕС…СЂР°РЅРµРЅР°: {SPECIALIZATIONS[spec]}\n\n{_build_profile_text(profile)}",
            reply_markup=_build_profile_keyboard(profile)
        )

    elif query.data in ("profile_toggle_vo2max_lock", "profile_toggle_lactate_lock"):
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        profile = get_user_profile(db_user_id)
        if query.data == "profile_toggle_vo2max_lock":
            new_val = 0 if (profile or {}).get("vo2max_locked") else 1
            save_user_profile(db_user_id, vo2max_locked=new_val)
            msg_extra = "🔒 VO2max защищён от автообновления." if new_val else "🔓 VO2max будет обновляться из сервисов."
        else:
            new_val = 0 if (profile or {}).get("lactate_locked") else 1
            save_user_profile(db_user_id, lactate_locked=new_val)
            msg_extra = "🔒 Лактатный порог защищён от автообновления." if new_val else "🔓 Лактатный порог будет обновляться из сервисов."
        profile = get_user_profile(db_user_id)
        await query.edit_message_text(
            f"{msg_extra}\n\n{_build_profile_text(profile)}",
            reply_markup=_build_profile_keyboard(profile)
        )

    elif query.data == "ai_mode":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        prefs = get_preferences(db_user_id)
        current_mode = prefs.get("ai_mode", "smart") if prefs else "smart"
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("рџЏ  Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ", callback_data="main_menu")]])
        await query.edit_message_text(
            _build_mode_text(current_mode),
            reply_markup=_merge_keyboards(_build_mode_keyboard(current_mode), back_btn)
        )

    elif query.data in ("mode_set_deep", "mode_set_smart", "mode_set_fast", "mode_set_calc"):
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        new_mode = query.data.replace("mode_set_", "")
        set_preference(db_user_id, "ai_mode", new_mode)
        emoji, label, timing, _ = _MODE_INFO[new_mode]
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("рџЏ  Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ", callback_data="main_menu")]])
        await query.edit_message_text(
            f"вњ… Р РµР¶РёРј СЃРѕС…СЂР°РЅС‘РЅ: {emoji} {label} ({timing})\n\n{_build_mode_text(new_mode)}",
            reply_markup=_merge_keyboards(_build_mode_keyboard(new_mode), back_btn)
        )

    elif query.data in ("garmin_recovery_yes", "garmin_recovery_no"):
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        use = 1 if query.data == "garmin_recovery_yes" else 0
        set_preference(db_user_id, "use_garmin_recovery", use)
        whoop_connected = bool(get_token(db_user_id, "whoop"))
        if use:
            if whoop_connected:
                answer = (
                    "вњ… Garmin РІРєР»СЋС‡С‘РЅ РєР°Рє СЂРµР·РµСЂРІРЅС‹Р№ РёСЃС‚РѕС‡РЅРёРє.\n\n"
                    "Whoop РїРѕРґРєР»СЋС‡С‘РЅ Рё Р±СѓРґРµС‚ РІ РїСЂРёРѕСЂРёС‚РµС‚Рµ вЂ” "
                    "Garmin (Body Battery, HRV) РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ С‚РѕР»СЊРєРѕ РµСЃР»Рё РґР°РЅРЅС‹С… Whoop РЅРµС‚."
                )
            else:
                answer = "вњ… Р‘СѓРґСѓ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РґР°РЅРЅС‹Рµ Body Battery Рё HRV РёР· Garmin СѓС‚СЂРѕРј."
        else:
            answer = (
                "РџРѕРЅСЏР», РґР°РЅРЅС‹Рµ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ РёР· Garmin РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊСЃСЏ РЅРµ Р±СѓРґСѓС‚.\n"
                "РР·РјРµРЅРёС‚СЊ РјРѕР¶РЅРѕ С‡РµСЂРµР· /status."
            )
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        await query.edit_message_text(
            answer,
            reply_markup=_build_screen3_keyboard(db_user_id)
        )

    elif query.data == "toggle_garmin_recovery":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        prefs = get_preferences(db_user_id)
        current = prefs.get("use_garmin_recovery", True) if prefs else True
        new_val = 0 if current else 1
        set_preference(db_user_id, "use_garmin_recovery", new_val)
        await _show_status(query, db_user_id)

    elif query.data == "notifications":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        prefs = get_preferences(db_user_id)
        await query.edit_message_text(
            _build_notifications_text(prefs),
            reply_markup=_build_notifications_keyboard(prefs)
        )

    elif query.data.startswith("notif_on_") or query.data.startswith("notif_off_"):
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        parts = query.data.split("_", 2)
        action, key = parts[1], parts[2]
        new_val = 1 if action == "on" else 0
        set_preference(db_user_id, key, new_val)
        prefs = get_preferences(db_user_id)
        await query.edit_message_text(
            _build_notifications_text(prefs),
            reply_markup=_build_notifications_keyboard(prefs)
        )

    elif query.data == "fit_dl":
        data = _fit_data.get(user.id)
        if not data:
            await context.bot.send_message(user.id, "вЏ± Р”Р°РЅРЅС‹Рµ СѓСЃС‚Р°СЂРµР»Рё. Р—Р°РїСЂРѕСЃРё СЂРµРєРѕРјРµРЅРґР°С†РёСЋ Р·Р°РЅРѕРІРѕ (/workout РёР»Рё /long)")
            return
        try:
            from fit_generator import (
                build_garmin_interval_workout, build_garmin_long_run_workout,
                workout_filename,
            )
            import json, io
            # РСЃРїРѕР»СЊР·СѓРµРј JSON РѕС‚ DeepSeek РµСЃР»Рё РµСЃС‚СЊ, РёРЅР°С‡Рµ РїР°СЂСЃРµСЂ
            wkt = data.get('garmin_json')
            if not wkt:
                if data['type'] == 'interval':
                    wkt = build_garmin_interval_workout(
                        data['workout'], data['recommended_group'], data.get('recommended_pace', ''))
                else:
                    wkt = build_garmin_long_run_workout(
                        data['workout'], data['recommended_group'],
                        data.get('strategy', 'even'), data.get('first_half_pace', ''), data.get('second_half_pace'))
            fname = workout_filename(data['workout'].get('workout_date', ''), data['recommended_group'])
            json_bytes = json.dumps(wkt, ensure_ascii=False, indent=2).encode('utf-8')
            await context.bot.send_document(
                user.id,
                document=io.BytesIO(json_bytes),
                filename=fname,
                caption=f"рџЏѓ <b>{fname}</b>\n\nРРјРїРѕСЂС‚РёСЂСѓР№ РІ Garmin Connect: РўСЂРµРЅРёСЂРѕРІРєРё в†’ вћ• в†’ РР· С„Р°Р№Р»Р°.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"JSON generation error for {user.id}: {e}")
            await context.bot.send_message(user.id, f"вќЊ РћС€РёР±РєР° РіРµРЅРµСЂР°С†РёРё JSON: {type(e).__name__}: {e}")

    elif query.data == "fit_up":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        if not get_token(db_user_id, "garmin"):
            await context.bot.send_message(user.id,
                "вќЊ Garmin РЅРµ РїРѕРґРєР»СЋС‡С‘РЅ.\n\nРСЃРїРѕР»СЊР·СѓР№ /connect_garmin С‡С‚РѕР±С‹ РїРѕРґРєР»СЋС‡РёС‚СЊ Р°РєРєР°СѓРЅС‚.")
            return
        data = _fit_data.get(user.id)
        if not data:
            await context.bot.send_message(user.id, "вЏ± Р”Р°РЅРЅС‹Рµ СѓСЃС‚Р°СЂРµР»Рё. Р—Р°РїСЂРѕСЃРё СЂРµРєРѕРјРµРЅРґР°С†РёСЋ Р·Р°РЅРѕРІРѕ (/workout РёР»Рё /long)")
            return
        try:
            from fit_generator import build_garmin_interval_workout, build_garmin_long_run_workout
            from garmin import upload_workout as garmin_upload_workout
            # РСЃРїРѕР»СЊР·СѓРµРј JSON РѕС‚ DeepSeek РµСЃР»Рё РµСЃС‚СЊ, РёРЅР°С‡Рµ РїР°СЂСЃРµСЂ
            wkt_json = data.get('garmin_json')
            if not wkt_json:
                if data['type'] == 'interval':
                    wkt_json = build_garmin_interval_workout(
                        data['workout'], data['recommended_group'], data.get('recommended_pace', ''))
                else:
                    wkt_json = build_garmin_long_run_workout(
                        data['workout'], data['recommended_group'],
                        data.get('strategy', 'even'), data.get('first_half_pace', ''), data.get('second_half_pace'))
            ok = await garmin_upload_workout(db_user_id, wkt_json)
            if ok:
                name = wkt_json.get('workoutName', '')
                await context.bot.send_message(user.id,
                    f"вњ… <b>РўСЂРµРЅРёСЂРѕРІРєР° Р·Р°РіСЂСѓР¶РµРЅР° РІ Garmin Connect!</b>\n\n"
                    f"рџ“‹ {name}\n\n"
                    f"РћС‚РєСЂРѕР№ РїСЂРёР»РѕР¶РµРЅРёРµ Garmin Connect в†’ РўСЂРµРЅРёСЂРѕРІРєРё Рё РїР»Р°РЅС‹ в†’ РўСЂРµРЅРёСЂРѕРІРєРё.",
                    parse_mode="HTML")
            else:
                await context.bot.send_message(user.id,
                    "вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РІ Garmin Connect.\n\n"
                    "РџРѕРїСЂРѕР±СѓР№ СЃРєР°С‡Р°С‚СЊ JSON РєРЅРѕРїРєРѕР№ рџ“Ґ Рё РёРјРїРѕСЂС‚РёСЂРѕРІР°С‚СЊ РІСЂСѓС‡РЅСѓСЋ.")
        except Exception as e:
            logger.error(f"Garmin upload error for {user.id}: {e}")
            await context.bot.send_message(user.id,
                f"вќЊ РћС€РёР±РєР° Р·Р°РіСЂСѓР·РєРё РІ Garmin: {type(e).__name__}")

    elif query.data == "help":
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("рџЏ  Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ", callback_data="main_menu")]])
        await query.edit_message_text(_build_help_text(user.id in ADMIN_TELEGRAM_IDS), reply_markup=back_btn)

    elif query.data.startswith("preprocess_set_"):
        if user.id not in ADMIN_TELEGRAM_IDS:
            return
        new_mode = query.data.replace("preprocess_set_", "")
        set_preprocess_mode(new_mode)
        await query.edit_message_text(
            _build_preprocess_text(new_mode),
            reply_markup=_build_preprocess_keyboard(new_mode),
        )

    elif query.data in ("analyze_interval", "analyze_long"):
        if user.id not in ADMIN_TELEGRAM_IDS:
            return
        is_long = query.data == "analyze_long"
        await query.edit_message_text(
            f"рџ”¬ РС‰Сѓ {'Long Run' if is_long else 'РёРЅС‚РµСЂРІР°Р»СЊРЅСѓСЋ'} С‚СЂРµРЅРёСЂРѕРІРєСѓ РІ РєР°РЅР°Р»Рµ..."
        )
        workout = await (find_next_long_run() if is_long else find_next_workout(only_interval=True))
        if not workout:
            await query.edit_message_text("рџ” РќРµ РЅР°С€С‘Р» РїРѕРґС…РѕРґСЏС‰СѓСЋ С‚СЂРµРЅРёСЂРѕРІРєСѓ РІ РєР°РЅР°Р»Рµ.")
            return
        await _run_analyze_and_show(workout, query, context)

    # в”Ђв”Ђ РћР‘Р РђРўРќРђРЇ РЎР’РЇР—Р¬ в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

    elif query.data == "feedback_show":
        await query.edit_message_text("Р’С‹Р±РµСЂРё С‚РёРї:", reply_markup=_build_feedback_keyboard())

    elif query.data in ("feedback_bug", "feedback_feature"):
        fb_type = "bug" if query.data == "feedback_bug" else "feature"
        type_label = "РїСЂРѕР±Р»РµРјСѓ" if fb_type == "bug" else "РёРґРµСЋ"
        context.user_data["awaiting_feedback"] = fb_type
        await query.edit_message_text(f"РћРїРёС€Рё {type_label}:")

    # в”Ђв”Ђ РћР¦Р•РќРљРђ Р Р•РљРћРњР•РќР”РђР¦РР в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

    elif query.data == "rate_show":
        data = _rating_data.get(user.id)
        if not data:
            await context.bot.send_message(
                user.id, "вЏ± Р”Р°РЅРЅС‹Рµ СѓСЃС‚Р°СЂРµР»Рё. Р—Р°РїСЂРѕСЃРё СЂРµРєРѕРјРµРЅРґР°С†РёСЋ Р·Р°РЅРѕРІРѕ (/workout РёР»Рё /long)."
            )
            return
        context.user_data["rating_pending"] = dict(data)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(str(i), callback_data=f"rate_{i}") for i in range(1, 6)],
            [InlineKeyboardButton(str(i), callback_data=f"rate_{i}") for i in range(6, 11)],
            [InlineKeyboardButton("рџЏ  Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ", callback_data="main_menu_new")],
        ])
        await query.edit_message_text(
            "РћС†РµРЅРё СЂРµРєРѕРјРµРЅРґР°С†РёСЋ:\n1 вЂ” РїР»РѕС…Рѕ, 10 вЂ” РѕС‚Р»РёС‡РЅРѕ",
            reply_markup=keyboard
        )

    elif query.data.startswith("rate_") and query.data[5:].isdigit():
        rating = int(query.data[5:])
        ctx = context.user_data.get("rating_pending", {})
        ctx["rating"] = rating
        context.user_data["rating_pending"] = ctx
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("вњЏпёЏ РќР°РїРёСЃР°С‚СЊ РєРѕРјРјРµРЅС‚Р°СЂРёР№", callback_data="rate_comment"),
             InlineKeyboardButton("РџСЂРѕРїСѓСЃС‚РёС‚СЊ", callback_data="rate_skip")],
            [InlineKeyboardButton("рџЏ  Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ", callback_data="main_menu_new")],
        ])
        await query.edit_message_text(
            f"РћС†РµРЅРєР° {rating}/10 вњ…\n\nРҐРѕС‡РµС€СЊ РґРѕР±Р°РІРёС‚СЊ РєРѕРјРјРµРЅС‚Р°СЂРёР№? (РЅРµРѕР±СЏР·Р°С‚РµР»СЊРЅРѕ)",
            reply_markup=keyboard
        )

    elif query.data == "rate_comment":
        context.user_data["awaiting_rating_comment"] = True
        await query.edit_message_text("РќР°РїРёС€Рё РєРѕРјРјРµРЅС‚Р°СЂРёР№:")

    elif query.data == "rate_skip":
        ctx = context.user_data.pop("rating_pending", {})
        rating = ctx.get("rating", 0)
        if rating:
            db_user_id = get_or_create_user(user.id, user.full_name, user.username)
            save_rating(db_user_id, ctx.get("workout_date", ""), rating, ctx.get("ai_mode", ""), None)
            if rating <= 5:
                uname = f" (@{user.username})" if user.username else ""
                await _notify_admin(
                    context.bot,
                    f"в­ђ РќРёР·РєР°СЏ РѕС†РµРЅРєР°: {rating}/10\n"
                    f"РћС‚: {user.full_name}{uname}\n"
                    f"РўСЂРµРЅРёСЂРѕРІРєР°: {ctx.get('workout_date', 'вЂ”')}\n"
                    f"Р РµР¶РёРј: {ctx.get('ai_mode', 'вЂ”')}\n"
                    f"РљРѕРјРјРµРЅС‚Р°СЂРёР№: РЅРµС‚"
                )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("рџЏ  Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ", callback_data="main_menu_new")
        ]])
        await query.edit_message_text("вњ… РЎРїР°СЃРёР±Рѕ Р·Р° РѕС†РµРЅРєСѓ!", reply_markup=keyboard)


# в”Ђв”Ђ РћР‘Р РђР‘РћРўРљРђ РўР•РљРЎРўРђ в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    _mark_user_active_if_needed(user.id, user.full_name, user.username)

    # РљРѕРґ Whoop вЂ” РїРѕР»СЊР·РѕРІР°С‚РµР»СЊ РІСЃС‚Р°РІР»СЏРµС‚ URL СЃ httpbin.org
    if context.user_data.get("awaiting_whoop_code"):
        import re
        from whoop import exchange_code as whoop_exchange
        code_match = re.search(r'[?&]code=([^&]+)', text)
        code = code_match.group(1) if code_match else text.strip()
        msg = await update.message.reply_text("вЏі РџРѕРґРєР»СЋС‡Р°СЋ Whoop...")
        try:
            import time as _t
            token_data = await whoop_exchange(code)
            if "access_token" not in token_data:
                await msg.edit_text("вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРґРєР»СЋС‡РёС‚СЊ Whoop. РџРѕРїСЂРѕР±СѓР№ /connect_whoop")
                return
            db_user_id = get_or_create_user(user.id, user.full_name, user.username)
            save_token(db_user_id, "whoop",
                token_data["access_token"],
                token_data.get("refresh_token"),
                str(int(_t.time()) + token_data.get("expires_in", 3600))
            )
            context.user_data["awaiting_whoop_code"] = False
            db_user_id2 = get_or_create_user(user.id, user.full_name, user.username)
            await msg.edit_text(
                "вњ… Whoop РїРѕРґРєР»СЋС‡С‘РЅ! РЈС‚СЂРµРЅРЅРёРµ СЂРµРєРѕРјРµРЅРґР°С†РёРё С‚РµРїРµСЂСЊ С‚РѕС‡РЅРµРµ.",
                reply_markup=_build_screen3_keyboard(db_user_id2)
            )
        except Exception as e:
            logger.error(f"Whoop auth error: {e}")
            await msg.edit_text("вќЊ РћС€РёР±РєР°. РџРѕРїСЂРѕР±СѓР№ /connect_whoop")
        return

    # Р’РІРѕРґ РґР°РЅРЅС‹С… РїСЂРѕС„РёР»СЏ
    elif context.user_data.get("awaiting_profile") == "set_vo2max":
        import re
        if not re.match(r'^\d+(?:[.,]\d+)?$', text):
            await update.message.reply_text("Р’РІРµРґРё С‡РёСЃР»Рѕ, РЅР°РїСЂРёРјРµСЂ: 53")
            return
        vo2max = float(text.replace(',', '.'))
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        save_user_profile(db_user_id, vo2max=vo2max, vo2max_source="manual")
        try:
            zones.recalculate_and_save(db_user_id)
        except Exception as e:
            logger.warning(f"Zones recalc error (manual vo2max) for {user.id}: {e}")
        context.user_data.pop("awaiting_profile")
        profile = get_user_profile(db_user_id)
        await update.message.reply_text(
            f"вњ… VO2max СЃРѕС…СЂР°РЅС‘РЅ: {vo2max} РјР»/РєРі/РјРёРЅ\n\n{_build_profile_text(profile)}",
            reply_markup=_build_profile_keyboard(profile)
        )

    elif context.user_data.get("awaiting_profile") == "set_lactate_pace":
        import re
        pace_match = re.match(r'^(\d+:\d{2})$', text.strip())
        if not pace_match:
            await update.message.reply_text("РќРµ СЂР°СЃРїРѕР·РЅР°Р» С‚РµРјРї. Р¤РѕСЂРјР°С‚: 4:17")
            return
        pace = pace_match.group(1)
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        save_user_profile(db_user_id, lactate_threshold_pace=pace, lactate_source="manual")
        try:
            zones.recalculate_and_save(db_user_id)
        except Exception as e:
            logger.warning(f"Zones recalc error (manual lactate) for {user.id}: {e}")
        context.user_data["awaiting_profile"] = "set_lactate_hr"
        context.user_data["lactate_pace"] = pace
        await update.message.reply_text(
            f"РўРµРјРї {pace} СЃРѕС…СЂР°РЅС‘РЅ.\n\nРўРµРїРµСЂСЊ РІРІРµРґРё РїСѓР»СЊСЃ РЅР° Р»Р°РєС‚Р°С‚РЅРѕРј РїРѕСЂРѕРіРµ.\n\nРќР°РїСЂРёРјРµСЂ: 174"
        )

    elif context.user_data.get("awaiting_profile") == "set_lactate_hr":
        import re
        if not re.match(r'^\d{2,3}$', text.strip()):
            await update.message.reply_text("Р’РІРµРґРё РїСѓР»СЊСЃ С‡РёСЃР»РѕРј, РЅР°РїСЂРёРјРµСЂ: 174")
            return
        hr = int(text.strip())
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        save_user_profile(db_user_id, lactate_threshold_hr=hr, lactate_source="manual")
        pace = context.user_data.pop("lactate_pace", "")
        context.user_data.pop("awaiting_profile")
        profile = get_user_profile(db_user_id)
        await update.message.reply_text(
            f"вњ… Р›Р°РєС‚Р°С‚РЅС‹Р№ РїРѕСЂРѕРі СЃРѕС…СЂР°РЅС‘РЅ: {pace} РјРёРЅ/РєРј РїСЂРё Р§РЎРЎ {hr} СѓРґ/РјРёРЅ\n\n{_build_profile_text(profile)}",
            reply_markup=_build_profile_keyboard(profile)
        )

    # Email РґР»СЏ Garmin
    elif context.user_data.get("awaiting_garmin") == "email":
        context.user_data["garmin_email"] = text.strip()
        context.user_data["awaiting_garmin"] = "password"
        await update.message.reply_text(
            "Р’РІРµРґРё РїР°СЂРѕР»СЊ РѕС‚ Garmin Connect:\n\n"
            "РЎРѕРѕР±С‰РµРЅРёРµ СЃ РїР°СЂРѕР»РµРј Р±СѓРґРµС‚ СѓРґР°Р»РµРЅРѕ СЃСЂР°Р·Сѓ РїРѕСЃР»Рµ РѕС‚РїСЂР°РІРєРё."
        )

    # РџР°СЂРѕР»СЊ РґР»СЏ Garmin
    elif context.user_data.get("awaiting_garmin") == "password":
        from garmin import connect as garmin_connect, get_vo2max, get_training_readiness
        email = context.user_data.pop("garmin_email", "")
        password = text.strip()
        context.user_data.pop("awaiting_garmin", None)

        # РЈРґР°Р»СЏРµРј СЃРѕРѕР±С‰РµРЅРёРµ СЃ РїР°СЂРѕР»РµРј
        try:
            await update.message.delete()
        except Exception:
            pass

        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        msg = await update.effective_chat.send_message("вЏі РџРѕРґРєР»СЋС‡Р°СЋСЃСЊ Рє Garmin Connect...")
        try:
            await garmin_connect(db_user_id, email, password)
            save_user_profile(db_user_id, garmin_email=email, garmin_password=password)

            from garmin import get_body_battery, get_hrv_status, get_lactate_threshold
            vo2max, readiness, body_battery, hrv, lt = await asyncio.gather(
                get_vo2max(db_user_id),
                get_training_readiness(db_user_id),
                get_body_battery(db_user_id),
                get_hrv_status(db_user_id),
                get_lactate_threshold(db_user_id),
                return_exceptions=True,
            )

            garmin_vo2max_found = not isinstance(vo2max, Exception) and vo2max is not None
            garmin_lt_found     = not isinstance(lt, Exception) and lt

            if garmin_vo2max_found:
                save_user_profile(db_user_id, vo2max=vo2max, vo2max_source="garmin")
            if garmin_lt_found:
                save_user_profile(db_user_id,
                    lactate_threshold_pace=lt["pace"],
                    lactate_threshold_hr=lt["hr"],
                    lactate_source="auto")
            if garmin_vo2max_found or garmin_lt_found:
                try:
                    zones.recalculate_and_save(db_user_id)
                except Exception as e:
                    logger.warning(f"Zones recalc error (garmin connect) for {user.id}: {e}")

            lines = ["вњ… Garmin РїРѕРґРєР»СЋС‡С‘РЅ!\n"]

            if garmin_vo2max_found or garmin_lt_found or (not isinstance(body_battery, Exception) and body_battery is not None):
                lines.append("Р—Р°РіСЂСѓР¶РµРЅРѕ РёР· Garmin:")
                if garmin_vo2max_found:
                    lines.append(f"рџ“Љ VO2max: {vo2max:.1f} РјР»/РєРі/РјРёРЅ")
                else:
                    lines.append("рџ“Љ VO2max: РЅРµ РЅР°Р№РґРµРЅ вЂ” СѓРєР°Р¶Рё РІСЂСѓС‡РЅСѓСЋ РІ /profile")
                if garmin_lt_found:
                    lines.append(f"вљЎ Р›Р°РєС‚Р°С‚РЅС‹Р№ РїРѕСЂРѕРі: {lt['pace']} РјРёРЅ/РєРј РїСЂРё Р§РЎРЎ {lt['hr']}")
                if not isinstance(body_battery, Exception) and body_battery is not None:
                    lines.append(f"рџ”‹ Body Battery: {body_battery}/100")
                if not isinstance(hrv, Exception) and hrv:
                    lines.append(f"рџ’— HRV: {hrv.get('hrv_last_night', 'вЂ”')} РјСЃ (СЃСЂРµРґРЅРµРµРЅРµРґРµР»СЊРЅРѕРµ: {hrv.get('hrv_weekly_avg', 'вЂ”')})")
                if not isinstance(readiness, Exception) and readiness and readiness.get("score") is not None:
                    lines.append(f"рџЋЇ Training Readiness: {readiness['score']}/100 ({readiness.get('level', '')})")
            else:
                lines.append("рџ“Љ VO2max РЅРµ РЅР°Р№РґРµРЅ РІ РґР°РЅРЅС‹С….")
                lines.append("РЈРєР°Р¶Рё РµРіРѕ РІСЂСѓС‡РЅСѓСЋ РІ РїСЂРѕС„РёР»Рµ в†’ /profile")

            if garmin_vo2max_found:
                lines.append("\nРҐРѕС‡РµС€СЊ СѓС‚РѕС‡РЅРёС‚СЊ Р»Р°РєС‚Р°С‚РЅС‹Р№ РїРѕСЂРѕРі? в†’ /profile")
                lines.append("РР»Рё СЃСЂР°Р·Сѓ РїРѕРїСЂРѕР±СѓР№ /workout")

            lines.append(
                "\nРўС‹ РЅРѕСЃРёС€СЊ Garmin РїРѕСЃС‚РѕСЏРЅРЅРѕ (РІРєР»СЋС‡Р°СЏ СЃРѕРЅ)?\n"
                "Р­С‚Рѕ РІР»РёСЏРµС‚ РЅР° С‚Рѕ, РёСЃРїРѕР»СЊР·СѓРµРј Р»Рё Body Battery Рё HRV СѓС‚СЂРѕРј."
            )
            keyboard = _merge_keyboards(
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("вњ… Р”Р°, РїРѕСЃС‚РѕСЏРЅРЅРѕ", callback_data="garmin_recovery_yes"),
                    InlineKeyboardButton("рџЏѓ РўРѕР»СЊРєРѕ РЅР° С‚СЂРµРЅРёСЂРѕРІРєР°С…", callback_data="garmin_recovery_no"),
                ]]),
                InlineKeyboardMarkup([[InlineKeyboardButton("в†ђ РЎРµСЂРІРёСЃС‹", callback_data="show_services")]])
            )
            await msg.edit_text("\n".join(lines), reply_markup=keyboard)

            n = count_users_with_service("garmin")
            uname = f" (@{user.username})" if user.username else ""
            await _notify_admin(
                context.bot,
                f"рџ”µ {user.full_name}{uname} РїРѕРґРєР»СЋС‡РёР» Garmin\n"
                f"Р’СЃРµРіРѕ СЃ Garmin: {n}"
            )
        except Exception as e:
            logger.error(f"Garmin auth error: {e}")
            await msg.edit_text(
                f"вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРґРєР»СЋС‡РёС‚СЊ Garmin Connect.\n"
                f"РџСЂРѕРІРµСЂСЊ РїСЂР°РІРёР»СЊРЅРѕСЃС‚СЊ email Рё РїР°СЂРѕР»СЏ, Р·Р°С‚РµРј РїРѕРїСЂРѕР±СѓР№ /connect_garmin СЃРЅРѕРІР°.\n\n"
                f"РћС€РёР±РєР°: {type(e).__name__}: {e}"
            )

    # Email РґР»СЏ COROS
    elif context.user_data.get("awaiting_coros") == "email":
        context.user_data["coros_email"] = text.strip()
        context.user_data["awaiting_coros"] = "password"
        await update.message.reply_text(
            "Р’РІРµРґРё РїР°СЂРѕР»СЊ РѕС‚ COROS:\n\n"
            "РЎРѕРѕР±С‰РµРЅРёРµ СЃ РїР°СЂРѕР»РµРј Р±СѓРґРµС‚ СѓРґР°Р»РµРЅРѕ СЃСЂР°Р·Сѓ РїРѕСЃР»Рµ РѕС‚РїСЂР°РІРєРё."
        )

    # РџР°СЂРѕР»СЊ РґР»СЏ COROS
    elif context.user_data.get("awaiting_coros") == "password":
        import coros as _coros
        email = context.user_data.pop("coros_email", "")
        password = text.strip()
        context.user_data.pop("awaiting_coros", None)

        # РЈРґР°Р»СЏРµРј СЃРѕРѕР±С‰РµРЅРёРµ СЃ РїР°СЂРѕР»РµРј
        try:
            await update.message.delete()
        except Exception:
            pass

        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        msg = await update.effective_chat.send_message("вЏі РџРѕРґРєР»СЋС‡Р°СЋСЃСЊ Рє COROS...")
        try:
            await _coros.connect(db_user_id, email, password)
            save_user_profile(db_user_id, coros_email=email, coros_password=password)

            vo2max, training_load, hrv = await asyncio.gather(
                _coros.get_vo2max(db_user_id),
                _coros.get_training_load(db_user_id),
                _coros.get_hrv_status(db_user_id),
                return_exceptions=True,
            )

            coros_vo2max_found = not isinstance(vo2max, Exception) and vo2max is not None

            if coros_vo2max_found:
                save_user_profile(db_user_id, vo2max=vo2max, vo2max_source="coros")
                try:
                    zones.recalculate_and_save(db_user_id)
                except Exception as e:
                    logger.warning(f"Zones recalc error (coros connect) for {user.id}: {e}")

            lines = ["вњ… COROS РїРѕРґРєР»СЋС‡С‘РЅ!\n"]
            lines.append("Р—Р°РіСЂСѓР¶РµРЅРѕ РёР· COROS:")

            if coros_vo2max_found:
                lines.append(f"рџ“Љ VO2max: {vo2max} РјР»/РєРі/РјРёРЅ")
            else:
                lines.append("рџ“Љ VO2max: РЅРµ РЅР°Р№РґРµРЅ вЂ” СѓРєР°Р¶Рё РІСЂСѓС‡РЅСѓСЋ РІ /profile")

            if not isinstance(training_load, Exception) and training_load:
                ctl = training_load.get("ctl")
                atl = training_load.get("atl")
                tsb = training_load.get("tsb")
                if ctl is not None:
                    lines.append(f"рџ“€ Training Load: CTL={ctl}, ATL={atl}, TSB={tsb}")

            if not isinstance(hrv, Exception) and hrv:
                hrv_last = hrv.get("hrv_last_night")
                hrv_avg  = hrv.get("hrv_weekly_avg")
                if hrv_last:
                    lines.append(f"рџ’— HRV: {hrv_last} РјСЃ (СЃСЂРµРґРЅРµРµРЅРµРґРµР»СЊРЅРѕРµ: {hrv_avg})")

            if coros_vo2max_found:
                lines.append("\nРҐРѕС‡РµС€СЊ СѓС‚РѕС‡РЅРёС‚СЊ Р»Р°РєС‚Р°С‚РЅС‹Р№ РїРѕСЂРѕРі? в†’ /profile")
                lines.append("РР»Рё СЃСЂР°Р·Сѓ РїРѕРїСЂРѕР±СѓР№ /workout")

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("в†ђ РЎРµСЂРІРёСЃС‹", callback_data="show_services")
            ]])
            await msg.edit_text("\n".join(lines), reply_markup=keyboard)

            n = count_users_with_service("coros")
            uname = f" (@{user.username})" if user.username else ""
            await _notify_admin(
                context.bot,
                f"рџ”ґ {user.full_name}{uname} РїРѕРґРєР»СЋС‡РёР» COROS\n"
                f"Р’СЃРµРіРѕ СЃ COROS: {n}"
            )
        except Exception as e:
            logger.error(f"COROS auth error: {e}")
            await msg.edit_text(
                f"вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРґРєР»СЋС‡РёС‚СЊ COROS.\n"
                f"РџСЂРѕРІРµСЂСЊ РїСЂР°РІРёР»СЊРЅРѕСЃС‚СЊ email Рё РїР°СЂРѕР»СЏ, Р·Р°С‚РµРј РїРѕРїСЂРѕР±СѓР№ /connect_coros СЃРЅРѕРІР°.\n\n"
                f"РћС€РёР±РєР°: {type(e).__name__}: {e}"
            )

    # в”Ђв”Ђ РћР‘Р РђРўРќРђРЇ РЎР’РЇР—Р¬ (С‚РµРєСЃС‚) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

    elif context.user_data.get("awaiting_feedback"):
        fb_type = context.user_data.pop("awaiting_feedback")
        type_label = "РџСЂРѕР±Р»РµРјР°" if fb_type == "bug" else "РРґРµСЏ"
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        save_feedback(db_user_id, fb_type, text)
        uname = f" (@{user.username})" if user.username else ""
        await _notify_admin(
            context.bot,
            f"рџ’¬ РћР±СЂР°С‚РЅР°СЏ СЃРІСЏР·СЊ [{type_label}]\nРћС‚: {user.full_name}{uname}\n\n{text}"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("рџЏ  Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ", callback_data="main_menu_new")
        ]])
        await update.message.reply_text("вњ… РЎРїР°СЃРёР±Рѕ! РЎРѕРѕР±С‰РµРЅРёРµ РѕС‚РїСЂР°РІР»РµРЅРѕ.", reply_markup=keyboard)
        return

    # в”Ђв”Ђ РљРћРњРњР•РќРўРђР РР™ Рљ РћР¦Р•РќРљР• в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

    elif context.user_data.get("awaiting_rating_comment"):
        context.user_data.pop("awaiting_rating_comment")
        ctx = context.user_data.pop("rating_pending", {})
        rating = ctx.get("rating", 0)
        if rating:
            db_user_id = get_or_create_user(user.id, user.full_name, user.username)
            save_rating(db_user_id, ctx.get("workout_date", ""), rating, ctx.get("ai_mode", ""), text)
            if rating <= 5:
                uname = f" (@{user.username})" if user.username else ""
                await _notify_admin(
                    context.bot,
                    f"в­ђ РќРёР·РєР°СЏ РѕС†РµРЅРєР°: {rating}/10\n"
                    f"РћС‚: {user.full_name}{uname}\n"
                    f"РўСЂРµРЅРёСЂРѕРІРєР°: {ctx.get('workout_date', 'вЂ”')}\n"
                    f"Р РµР¶РёРј: {ctx.get('ai_mode', 'вЂ”')}\n"
                    f"РљРѕРјРјРµРЅС‚Р°СЂРёР№: {text}"
                )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("рџЏ  Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ", callback_data="main_menu_new")
        ]])
        await update.message.reply_text("вњ… РЎРїР°СЃРёР±Рѕ Р·Р° РѕС†РµРЅРєСѓ!", reply_markup=keyboard)
        return


# в”Ђв”Ђ Р›РћР“РРљРђ Р Р•РљРћРњР•РќР”РђР¦РР™ в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

def _user_has_data(db_user_id: int) -> bool:
    """has_data: РµСЃС‚СЊ VO2max РІ РїСЂРѕС„РёР»Рµ РР›Р С…РѕС‚СЏ Р±С‹ РѕРґРёРЅ С‚РѕРєРµРЅ С‚СЂРµРєРµСЂР°."""
    profile = get_user_profile(db_user_id)
    if profile and profile.get("vo2max"):
        return True
    return any(get_token(db_user_id, s) for s in ("strava", "garmin", "coros", "polar"))


async def _send_recommendation(
    telegram_id: int, name: str,
    context: ContextTypes.DEFAULT_TYPE,
    long: bool = False,
    msg=None,
    live: dict | None = None,
):
    """Р3: СЂРµРєРѕРјРµРЅРґР°С†РёСЏ РёР· РєСЌС€Р° workout_analysis + recommend_group/recommend_long.
    find_next_* РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ РўРћР›Р¬РљРћ РґР»СЏ РґРµС‚РµРєС‚Р° СЃРІРµР¶РµСЃС‚Рё Р°РЅРѕРЅСЃР° (post_id/edit_date) Рё
    РєР°Рє РёСЃС‚РѕС‡РЅРёРє СѓРїСЂРѕС‰С‘РЅРЅРѕРіРѕ С‚РµРєСЃС‚Р° РґР»СЏ has_data=False вЂ” РќР• РґР»СЏ РїР°СЂСЃРёРЅРіР° СЂРµРєРѕРјРµРЅРґР°С†РёРё.
    live РјРѕР¶РЅРѕ РїРµСЂРµРґР°С‚СЊ Р·Р°СЂР°РЅРµРµ (СЂР°СЃСЃС‹Р»РєР° С„РµС‚С‡РёС‚ РѕРґРёРЅ СЂР°Р· РЅР° РІСЃРµС…).
    """
    import json as _json
    db_user_id = get_or_create_user(telegram_id, name)
    wtype = "long" if long else "interval"

    if live is None:
        live = await (find_next_long_run() if long else find_next_workout())
    cur_post = live.get("post_id") if live else None
    cur_date = live.get("workout_date") if live else None
    cur_edit = live.get("edit_date") if live else None

    row, status = get_latest_workout_analysis(wtype, cur_post, cur_date, cur_edit)

    async def _out(text, markup=None, parse_mode=None):
        if msg:
            await msg.edit_text(text, reply_markup=markup, parse_mode=parse_mode)
        else:
            await context.bot.send_message(telegram_id, text, reply_markup=markup, parse_mode=parse_mode)

    if status == "empty" or row is None:
        what = "Р±Р»РёР¶Р°Р№С€РµРіРѕ Long Run" if long else "Р±Р»РёР¶Р°Р№С€РµР№ С‚СЂРµРЅРёСЂРѕРІРєРё"
        await _out(f"рџ” РќРµ РЅР°С€С‘Р» Р°РЅРѕРЅСЃ {what} РІ РєР°РЅР°Р»Рµ. РџРѕРїСЂРѕР±СѓР№ РїРѕР·Р¶Рµ.")
        return

    # РџР»Р°С€РєСѓ past РќР• РґСѓР±Р»РёСЂСѓРµРј РґР»СЏ interval вЂ” РµС‘ СЂРёСЃСѓРµС‚ СЃР°Рј С„РѕСЂРјР°С‚С‚РµСЂ (is_past).
    banner = ""
    if status == "analyzing":
        banner = ("рџ”„ РќРѕРІС‹Р№ Р°РЅРѕРЅСЃ РїРѕСЏРІРёР»СЃСЏ, СЃРµР№С‡Р°СЃ РІ РїСЂРѕСЂР°Р±РѕС‚РєРµ вЂ” РѕР±РЅРѕРІРёС‚СЃСЏ С‡РµСЂРµР· РїР°СЂСѓ РјРёРЅСѓС‚.\n"
                  "РџРѕРєР° РїРѕРєР°Р·С‹РІР°СЋ РїСЂРµРґС‹РґСѓС‰СѓСЋ С‚СЂРµРЅРёСЂРѕРІРєСѓ.\n\n")
    elif status == "past" and long:
        banner = ("рџ“… Р‘СѓРґСѓС‰РёС… С‚СЂРµРЅРёСЂРѕРІРѕРє РїРѕРєР° РЅРµС‚. РџРѕРєР°Р·С‹РІР°СЋ РїРѕСЃР»РµРґРЅСЋСЋ РїСЂРѕС€РµРґС€СѓСЋ "
                  "(РґР»СЏ РѕР·РЅР°РєРѕРјР»РµРЅРёСЏ, РЅРµ РЅР° СЃРµРіРѕРґРЅСЏ).\n\n")

    # has_data=False в†’ СѓРїСЂРѕС‰С‘РЅРЅРѕРµ СѓРІРµРґРѕРјР»РµРЅРёРµ
    if not _user_has_data(db_user_id):
        if live:
            simple = _build_simple_workout_text(live)
        else:
            simple = (f"рџ“ў РўСЂРµРЅРёСЂРѕРІРєР° {row.get('workout_date', '')}\n\n"
                      "Р—Р°РїРѕР»РЅРё РїСЂРѕС„РёР»СЊ Рё РїРѕРґРєР»СЋС‡Рё С‚СЂРµРєРµСЂ, С‡С‚РѕР±С‹ РїРѕР»СѓС‡РёС‚СЊ СЂРµРєРѕРјРµРЅРґР°С†РёСЋ РіСЂСѓРїРїС‹. "
                      "РќРѕРІРёС‡РєР°Рј РїРѕРґРѕР№РґС‘С‚ РіСЂСѓРїРїР° Р·РґРѕСЂРѕРІСЊСЏ (Р±РµРі/С…РѕРґСЊР±Р°).")
        await _out(banner + simple)
        return

    # has_data=True в†’ РїРµСЂСЃРѕРЅР°Р»СЊРЅР°СЏ СЂРµРєРѕРјРµРЅРґР°С†РёСЏ РёР· РєСЌС€Р°
    try:
        analysis = _json.loads(row.get("analyzed_json") or "{}")
    except Exception:
        analysis = {}
    user_data = {
        "db_user_id": db_user_id,
        "specialization": (get_user_profile(db_user_id) or {}).get("specialization"),
        "recovery": await _get_recovery_data(db_user_id, force_fresh=True),
    }
    rec = (claude_advisor.recommend_long(analysis, user_data) if long
           else claude_advisor.recommend_group(analysis, user_data))
    if not rec or not rec.get("ok"):
        note = (rec or {}).get("note", "РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕР±СЂР°С‚СЊ СЂРµРєРѕРјРµРЅРґР°С†РёСЋ. РџРѕРїСЂРѕР±СѓР№ РїРѕР·Р¶Рµ.")
        await _out(banner + note)
        return

    _rating_data[telegram_id] = {
        "workout_date": analysis.get("workout_date", ""),
        "ai_mode": row.get("analysis_mode", ""),
    }
    rating_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("в­ђ РћС†РµРЅРёС‚СЊ СЂРµРєРѕРјРµРЅРґР°С†РёСЋ", callback_data="rate_show"),
    ]])
    final_markup = _merge_keyboards(rating_markup, get_main_keyboard(from_recommendation=True))

    # РЁР°РїРєР°/РїРѕРіРѕРґР° РёР· live (РґР»СЏ current/past СЃРѕРІРїР°РґР°РµС‚ СЃ РєСЌС€РµРј)
    workout_dict = dict(live) if live else {"workout_date": analysis.get("workout_date", "")}
    workout_dict["workout_type"] = "long" if long else "interval"
    workout_dict["is_past"] = (status == "past")
    workout_dict["even_pace_available"] = analysis.get("even_pace_available")
    weather = await get_weather_for_workout(
        workout_dict.get("location", ""), workout_dict.get("workout_date", ""),
        workout_dict.get("schedule", ""),
    )
    weather_line = format_weather_for_message(weather) if weather else ""
    has_tracker = any(get_token(db_user_id, s) for s in ("garmin", "coros", "polar", "strava"))

    # Р§РёСЃР»Р°/СЃС‚СЂСѓРєС‚СѓСЂР° вЂ” С„РѕСЂРјСѓР»Р°РјРё (РґРµС‚РµСЂРјРёРЅРёСЂРѕРІР°РЅРЅРѕ)
    if long:
        advice = claude_advisor.recommendation_to_long_advice(rec, analysis, user_data["recovery"])
    else:
        advice = claude_advisor.recommendation_to_advice(rec, analysis, user_data["recovery"])

    # Р РµР¶РёРј СЂРµРєРѕРјРµРЅРґР°С†РёРё (РЁР°Рі 2) РёР· РЅР°СЃС‚СЂРѕРµРє РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ; Р°РЅР°Р»РёР· (РЁР°Рі 1) РІСЃРµРіРґР° deep
    rec_mode = (get_preferences(db_user_id) or {}).get("ai_mode", "smart")
    main = rec.get("main_group") or {}
    facts = {
        "group": advice.get("recommended_group"),
        "pace": advice.get("recommended_pace") or advice.get("first_half_pace"),
        "zone": main.get("zone_disp") or main.get("zone_label"),
        "pct": main.get("pct"),
        "suitability": advice.get("suitability_percentages"),
        "specialization": rec.get("specialization_label") or user_data.get("specialization"),
        "character": rec.get("workout_character"),
        "recovery": claude_advisor._recovery_descriptor(user_data["recovery"]),
        "overall_purpose": analysis.get("overall_purpose"),
        "block_contrast": analysis.get("block_contrast"),
        "strategy": advice.get("run_strategy"),
        "first_half_pace": advice.get("first_half_pace"),
        "second_half_pace": advice.get("second_half_pace"),
    }
    import functools
    prose, stats2 = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(claude_advisor.generate_step2_prose, facts, rec_mode, long))
    # РР-РїСЂРѕР·Р° РїРѕРІРµСЂС… РїРѕСЃС‡РёС‚Р°РЅРЅРѕРіРѕ (С„РѕР»Р±СЌРє РЅР° С€Р°Р±Р»РѕРЅ, РµСЃР»Рё РјРѕРґРµР»СЊ РЅРµ РѕС‚РІРµС‚РёР»Р°)
    if prose.get("reason"):
        advice["reason"] = prose["reason"]
    if long and prose.get("strategy_reason"):
        advice["strategy_reason"] = prose["strategy_reason"]

    # Р¤СѓС‚РµСЂ РѕС‚СЂР°Р¶Р°РµС‚ РЎР’РћР™ СЌРєСЂР°РЅ вЂ” СЂРµР¶РёРј/СЃС‚РѕРёРјРѕСЃС‚СЊ СЂРµРєРѕРјРµРЅРґР°С†РёРё (РЁР°Рі 2), РЅРµ Р°РЅР°Р»РёР·Р°
    if long:
        body = claude_advisor.format_long_run_message(
            advice, workout_dict, stats=stats2, weather_line=weather_line, has_tracker=has_tracker)
    else:
        body = claude_advisor.format_evening_message(
            advice, workout_dict, stats=stats2, weather_line=weather_line, has_tracker=has_tracker)

    await _out(banner + body, final_markup, parse_mode="HTML")


async def _send_workout_recommendation(
    telegram_id: int, name: str,
    context: ContextTypes.DEFAULT_TYPE,
    msg=None
):
    db_user_id = get_or_create_user(telegram_id, name)

    global last_workout

    # 1. РќР°С…РѕРґРёРј С‚СЂРµРЅРёСЂРѕРІРєСѓ
    workout = await find_next_workout()
    if not workout:
        text = "рџ” РќРµ РЅР°С€С‘Р» Р°РЅРѕРЅСЃ Р±Р»РёР¶Р°Р№С€РµР№ С‚СЂРµРЅРёСЂРѕРІРєРё РІ РєР°РЅР°Р»Рµ. РџРѕРїСЂРѕР±СѓР№ РїРѕР·Р¶Рµ."
        if msg:
            await msg.edit_text(text)
        else:
            await context.bot.send_message(telegram_id, text)
        return

    last_workout = workout

    # if workout.get("is_past"):
    #     text = format_workout_message(workout)
    #     if msg:
    #         await msg.edit_text(text)
    #     else:
    #         await context.bot.send_message(telegram_id, text)
        # return

    # 2. Р”Р°РЅРЅС‹Рµ СЃРїРѕСЂС‚СЃРјРµРЅР°: Garmin в†’ COROS в†’ Polar в†’ Strava
    fitness = None

    fitness = await get_garmin_fitness_data(db_user_id)
    if fitness:
        logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: garmin РґР»СЏ user {db_user_id}")

    if not fitness:
        fitness = await get_coros_fitness_data(db_user_id)
        if fitness:
            logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: coros РґР»СЏ user {db_user_id}")

    if not fitness:
        fitness = await get_polar_fitness_data(db_user_id)
        if fitness:
            logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: polar РґР»СЏ user {db_user_id}")

    if not fitness:
        access_token = await ensure_valid_token(db_user_id)
        if access_token:
            try:
                fitness = await get_fitness_data(db_user_id, access_token)
                if fitness:
                    logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: strava РґР»СЏ user {db_user_id}")
            except Exception as e:
                logger.error(f"Strava error for {telegram_id}: {e}")

    if not fitness:
        _profile = get_user_profile(db_user_id)
        if _profile and _profile.get("vo2max") and _profile.get("lactate_threshold_pace"):
            fitness = {
                "source": "profile", "profile_only": True,
                "summary": "Р”Р°РЅРЅС‹Рµ С‚РѕР»СЊРєРѕ РёР· РїСЂРѕС„РёР»СЏ (Р±РµР· С‚СЂРµРєРµСЂР°)",
                "total_km": 0, "run_count": 0,
                "avg_pace": "вЂ”", "avg_hr": None, "fatigue_level": "unknown",
                "vo2max": _profile["vo2max"],
                "vo2max_source": _profile.get("vo2max_source") or "РїСЂРѕС„РёР»СЊ",
            }
            if _profile.get("lactate_threshold_pace"):
                fitness["lactate_threshold_pace"] = _profile["lactate_threshold_pace"]
            if _profile.get("lactate_threshold_hr"):
                fitness["lactate_threshold_hr"] = _profile["lactate_threshold_hr"]
            if _profile.get("gender"):
                fitness["gender"] = _profile["gender"]
            logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: profile_only (fast mode) РґР»СЏ user {db_user_id}")
        else:
            logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: РЅРµС‚ РґР°РЅРЅС‹С… РґР»СЏ user {db_user_id}")
            text = format_workout_message(workout)
            text += "\n\nРџРѕРґРєР»СЋС‡Рё Garmin (/connect_garmin), COROS (/connect_coros) РёР»Рё Polar (/connect_polar) РґР»СЏ СЂРµРєРѕРјРµРЅРґР°С†РёРё РіСЂСѓРїРїС‹"
            if msg:
                await msg.edit_text(text)
            else:
                await context.bot.send_message(telegram_id, text)
            return

    # 3. РџСЂРѕС„РёР»СЊ СЃРїРѕСЂС‚СЃРјРµРЅР° (VO2max / Р»Р°РєС‚Р°С‚РЅС‹Р№ РїРѕСЂРѕРі РёР· СЂСѓС‡РЅРѕРіРѕ РІРІРѕРґР°)
    profile = get_user_profile(db_user_id)
    if profile:
        if profile.get("vo2max") and not fitness.get("vo2max"):
            source = profile.get("vo2max_source")
            updated = profile.get("vo2max_updated_at") or ""
            stale = False
            if source in ("garmin", "coros") and updated:
                try:
                    from datetime import datetime as _dt
                    stale = (_dt.now() - _dt.fromisoformat(updated)).days > 30
                except Exception:
                    stale = True
            if not stale:
                fitness["vo2max"] = profile["vo2max"]
                fitness["vo2max_source"] = source or "РїСЂРѕС„РёР»СЊ"
        if profile.get("lactate_threshold_pace"):
            fitness["lactate_threshold_pace"] = profile["lactate_threshold_pace"]
        if profile.get("lactate_threshold_hr"):
            fitness["lactate_threshold_hr"] = profile["lactate_threshold_hr"]
        if profile.get("gender"):
            fitness["gender"] = profile["gender"]

    # 4. Р”Р°РЅРЅС‹Рµ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ (Whoop / Garmin) вЂ” РІСЃРµРіРґР° СЃРІРµР¶РёРµ РґР»СЏ /workout
    recovery = await _get_recovery_data(db_user_id, force_fresh=True)

    # 5. РџРѕРіРѕРґР°
    weather = await get_weather_for_workout(
        workout.get("location", ""),
        workout.get("workout_date", ""),
        workout.get("schedule", ""),
    )
    weather_line = format_weather_for_message(weather) if weather else ""
    weather_prompt = format_weather_for_prompt(weather) if weather else ""

    # 6. Groq СЂРµРєРѕРјРµРЅРґСѓРµС‚
    _profile_only = fitness.get("profile_only", False)
    prefs = get_preferences(db_user_id)
    ai_mode = prefs.get("ai_mode", "smart") if prefs else "smart"
    if _profile_only:
        ai_mode = "fast"  # profile-only в†’ РїСЂРёРЅСѓРґРёС‚РµР»СЊРЅРѕ fast
    wait_msg = {"deep": "рџ§  Р”СѓРјР°СЋ РЅР°Рґ СЂРµРєРѕРјРµРЅРґР°С†РёРµР№... (~2-3 РјРёРЅСѓС‚С‹)", "smart": "вљЎ РђРЅР°Р»РёР·РёСЂСѓСЋ... (~1-2 РјРёРЅСѓС‚С‹)", "fast": "рџ”Ґ РЎС‡РёС‚Р°СЋ Р±С‹СЃС‚СЂРѕ... (~30 СЃРµРєСѓРЅРґ)"}.get(ai_mode, "вљЎ РђРЅР°Р»РёР·РёСЂСѓСЋ... (~1-2 РјРёРЅСѓС‚С‹)")
    if msg:
        await msg.edit_text(wait_msg)
    prompt = build_evening_prompt(workout, fitness, recovery, weather_prompt=weather_prompt)
    # Р—Р°РїСѓСЃРєР°РµРј РІ executor С‡С‚РѕР±С‹ РЅРµ Р±Р»РѕРєРёСЂРѕРІР°С‚СЊ event loop
    import functools
    result = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(ask_groq, prompt, ai_mode))
    if result and result.get("timeout"):
        timeout_text = "вЏ± РњРѕРґРµР»СЊ РґСѓРјР°РµС‚ СЃР»РёС€РєРѕРј РґРѕР»РіРѕ. РџРѕРїСЂРѕР±СѓР№ вљЎ РЈРјРЅС‹Р№ СЂРµР¶РёРј (/mode)"
        if msg:
            await msg.edit_text(timeout_text, reply_markup=get_main_keyboard(from_recommendation=True))
        else:
            await context.bot.send_message(telegram_id, timeout_text)
        return
    advice = result["advice"] if result else None
    stats = result["stats"] if result else None
    garmin_json = result.get("garmin_workout") if result else None
    has_tracker = bool(
        get_token(db_user_id, "garmin") or
        get_token(db_user_id, "coros") or
        get_token(db_user_id, "polar") or
        get_token(db_user_id, "strava")
    )
    text = format_evening_message(advice, workout, stats=stats, weather_line=weather_line, profile_only=_profile_only, has_tracker=has_tracker)

    if advice:
        try:
            save_last_recommendation(db_user_id, advice, workout)
        except Exception as e:
            logger.error(f"РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ СЂРµРєРѕРјРµРЅРґР°С†РёСЋ: {e}")

    fit_markup = None
    rating_markup = None
    if msg and advice:
        rec_group = str(advice.get('recommended_group', ''))
        rec_pace  = str(advice.get('recommended_pace', ''))
        _fit_data[telegram_id] = {
            'type': 'interval',
            'workout': workout,
            'recommended_group': rec_group,
            'recommended_pace': rec_pace,
            'garmin_json': garmin_json,
        }
        _rating_data[telegram_id] = {
            'workout_date': workout.get('workout_date', ''),
            'ai_mode': ai_mode,
        }
        fit_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("рџ“Ґ РЎРєР°С‡Р°С‚СЊ JSON", callback_data="fit_dl"),
            InlineKeyboardButton("вЊљ Р—Р°РіСЂСѓР·РёС‚СЊ РІ Garmin", callback_data="fit_up"),
        ]])
        rating_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("в­ђ РћС†РµРЅРёС‚СЊ СЂРµРєРѕРјРµРЅРґР°С†РёСЋ", callback_data="rate_show"),
        ]])

    final_markup = _merge_keyboards(fit_markup, rating_markup, get_main_keyboard(from_recommendation=True))
    if msg:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=final_markup)
    else:
        await context.bot.send_message(telegram_id, text, parse_mode="HTML",
                                       reply_markup=get_main_keyboard(from_recommendation=True))


async def _send_morning_check(
    telegram_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    msg=None
):
    db_user_id = get_or_create_user(telegram_id, "")

    is_long_run_day = datetime.now().weekday() == 6  # РІРѕСЃРєСЂРµСЃРµРЅСЊРµ
    workout = await (find_next_long_run() if is_long_run_day else find_next_workout())
    if not workout:
        text = "рџ” РќРµ РЅР°С€С‘Р» С‚СЂРµРЅРёСЂРѕРІРєСѓ. РћС‚РґС‹С…Р°Р№!"
        if msg:
            await msg.edit_text(text)
        else:
            await context.bot.send_message(telegram_id, text)
        return

    # Р”Р°РЅРЅС‹Рµ СЃРїРѕСЂС‚СЃРјРµРЅР°: Garmin в†’ COROS в†’ Polar в†’ Strava
    fitness = None

    fitness = await get_garmin_fitness_data(db_user_id)
    if fitness:
        logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: garmin РґР»СЏ user {db_user_id}")

    if not fitness:
        fitness = await get_coros_fitness_data(db_user_id)
        if fitness:
            logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: coros РґР»СЏ user {db_user_id}")

    if not fitness:
        fitness = await get_polar_fitness_data(db_user_id)
        if fitness:
            logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: polar РґР»СЏ user {db_user_id}")

    if not fitness:
        access_token = await ensure_valid_token(db_user_id)
        if access_token:
            try:
                fitness = await get_fitness_data(db_user_id, access_token)
                if fitness:
                    logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: strava РґР»СЏ user {db_user_id}")
            except Exception as e:
                logger.error(f"Strava morning error: {e}")

    if not fitness:
        _profile = get_user_profile(db_user_id)
        if _profile and _profile.get("vo2max") and _profile.get("lactate_threshold_pace"):
            fitness = {
                "source": "profile", "profile_only": True,
                "summary": "Р”Р°РЅРЅС‹Рµ С‚РѕР»СЊРєРѕ РёР· РїСЂРѕС„РёР»СЏ (Р±РµР· С‚СЂРµРєРµСЂР°)",
                "total_km": 0, "run_count": 0,
                "avg_pace": "вЂ”", "avg_hr": None, "fatigue_level": "unknown",
                "vo2max": _profile["vo2max"],
                "vo2max_source": _profile.get("vo2max_source") or "РїСЂРѕС„РёР»СЊ",
            }
            if _profile.get("lactate_threshold_pace"):
                fitness["lactate_threshold_pace"] = _profile["lactate_threshold_pace"]
            if _profile.get("lactate_threshold_hr"):
                fitness["lactate_threshold_hr"] = _profile["lactate_threshold_hr"]
            if _profile.get("gender"):
                fitness["gender"] = _profile["gender"]
            logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: profile_only РґР»СЏ user {db_user_id}")
        else:
            logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: РЅРµС‚ РґР°РЅРЅС‹С… РґР»СЏ user {db_user_id}")
            fitness = {"summary": "РќРµС‚ РґР°РЅРЅС‹С…", "total_km": 0, "run_count": 0,
                       "avg_pace": "вЂ”", "avg_hr": None, "fatigue_level": "unknown"}

    # Whoop / Garmin / COROS
    recovery = await _get_recovery_data(db_user_id)

    if not recovery:
        has_garmin = bool(get_token(db_user_id, "garmin"))
        prefs = get_preferences(db_user_id)
        garmin_disabled = has_garmin and not (prefs.get("use_garmin_recovery", True) if prefs else True)
        if garmin_disabled:
            hint = "Garmin РїРѕРґРєР»СЋС‡С‘РЅ, РЅРѕ РґР°РЅРЅС‹Рµ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ РѕС‚РєР»СЋС‡РµРЅС‹ вЂ” РІРєР»СЋС‡Рё РІ /status"
        elif has_garmin:
            hint = "Garmin РїРѕРґРєР»СЋС‡С‘РЅ, РЅРѕ РґР°РЅРЅС‹Рµ Р·Р° РЅРѕС‡СЊ РЅРµ РїРѕР»СѓС‡РµРЅС‹ (РІРѕР·РјРѕР¶РЅРѕ, С‡Р°СЃС‹ РЅРµ СЃРёРЅС…СЂРѕРЅРёР·РёСЂРѕРІР°РЅС‹)"
        else:
            hint = "РџРѕРґРєР»СЋС‡Рё Whoop, Garmin (/connect_garmin) РёР»Рё COROS (/connect_coros) РґР»СЏ С‚РѕС‡РЅС‹С… СЂРµРєРѕРјРµРЅРґР°С†РёР№"
        text = (
            "вЂпёЏ Р”РѕР±СЂРѕРµ СѓС‚СЂРѕ!\n\n"
            f"РќРµС‚ РґР°РЅРЅС‹С… Рѕ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёРё. {hint}.\n\n"
            "РџСЂРёСЃР»СѓС€Р°Р№СЃСЏ Рє СЃРІРѕРёРј РѕС‰СѓС‰РµРЅРёСЏРј:\n"
            "вЂў Р•СЃР»Рё С‡СѓРІСЃС‚РІСѓРµС€СЊ СЃРµР±СЏ С…РѕСЂРѕС€Рѕ вЂ” РёРґРё РїРѕ РїР»Р°РЅСѓ\n"
            "вЂў Р•СЃР»Рё СѓСЃС‚Р°Р» вЂ” СЃРЅРёР·СЊ С‚РµРјРї РЅР° РіСЂСѓРїРїСѓ РЅРёР¶Рµ"
        )
        if msg:
            await msg.edit_text(text)
        else:
            await context.bot.send_message(telegram_id, text)
        return

    last_rec = get_last_recommendation(db_user_id, workout_date=workout.get("workout_date"))

    prefs = get_preferences(db_user_id)
    ai_mode = prefs.get("ai_mode", "smart") if prefs else "smart"
    prompt = build_morning_prompt(workout, fitness, recovery, last_rec=last_rec)
    import functools
    result = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(ask_groq, prompt, ai_mode))
    if result and result.get("timeout"):
        timeout_text = "вЏ± РњРѕРґРµР»СЊ РґСѓРјР°РµС‚ СЃР»РёС€РєРѕРј РґРѕР»РіРѕ. РџРѕРїСЂРѕР±СѓР№ вљЎ РЈРјРЅС‹Р№ СЂРµР¶РёРј (/mode)"
        if msg:
            await msg.edit_text(timeout_text)
        else:
            await context.bot.send_message(telegram_id, timeout_text)
        return
    advice = result["advice"] if result else None
    text = format_morning_message(advice, last_rec=last_rec)

    if msg:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=get_main_keyboard(from_recommendation=True))
    else:
        await context.bot.send_message(telegram_id, text, parse_mode="HTML",
                                       reply_markup=get_main_keyboard(from_recommendation=True))


async def _send_long_run_recommendation(
    telegram_id: int, name: str,
    context: ContextTypes.DEFAULT_TYPE,
    msg=None
):
    db_user_id = get_or_create_user(telegram_id, name)

    global last_long_run

    workout = await find_next_long_run()
    if not workout:
        text = "рџ” РќРµ РЅР°С€С‘Р» Р°РЅРѕРЅСЃ Long Run РІ РєР°РЅР°Р»Рµ. РџРѕРїСЂРѕР±СѓР№ РїРѕР·Р¶Рµ."
        if msg:
            await msg.edit_text(text)
        else:
            await context.bot.send_message(telegram_id, text)
        return

    last_long_run = workout

    fitness = None

    fitness = await get_garmin_fitness_data(db_user_id)
    if fitness:
        logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: garmin РґР»СЏ user {db_user_id}")

    if not fitness:
        fitness = await get_coros_fitness_data(db_user_id)
        if fitness:
            logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: coros РґР»СЏ user {db_user_id}")

    if not fitness:
        fitness = await get_polar_fitness_data(db_user_id)
        if fitness:
            logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: polar РґР»СЏ user {db_user_id}")

    if not fitness:
        access_token = await ensure_valid_token(db_user_id)
        if access_token:
            try:
                fitness = await get_fitness_data(db_user_id, access_token)
                if fitness:
                    logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: strava РґР»СЏ user {db_user_id}")
            except Exception as e:
                logger.error(f"Strava long run error for {telegram_id}: {e}")

    if not fitness:
        _profile = get_user_profile(db_user_id)
        if _profile and _profile.get("vo2max") and _profile.get("lactate_threshold_pace"):
            fitness = {
                "source": "profile", "profile_only": True,
                "summary": "Р”Р°РЅРЅС‹Рµ С‚РѕР»СЊРєРѕ РёР· РїСЂРѕС„РёР»СЏ (Р±РµР· С‚СЂРµРєРµСЂР°)",
                "total_km": 0, "run_count": 0,
                "avg_pace": "вЂ”", "avg_hr": None, "fatigue_level": "unknown",
                "vo2max": _profile["vo2max"],
                "vo2max_source": _profile.get("vo2max_source") or "РїСЂРѕС„РёР»СЊ",
            }
            if _profile.get("lactate_threshold_pace"):
                fitness["lactate_threshold_pace"] = _profile["lactate_threshold_pace"]
            if _profile.get("lactate_threshold_hr"):
                fitness["lactate_threshold_hr"] = _profile["lactate_threshold_hr"]
            if _profile.get("gender"):
                fitness["gender"] = _profile["gender"]
            logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: profile_only (fast mode) РґР»СЏ user {db_user_id}")
        else:
            logger.info(f"РСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…: РЅРµС‚ РґР°РЅРЅС‹С… РґР»СЏ user {db_user_id}")
            text = "рџ•ђ Long Run РЅР°Р№РґРµРЅ!\n\nРџРѕРґРєР»СЋС‡Рё Garmin (/connect_garmin), COROS (/connect_coros) РёР»Рё Polar (/connect_polar) РґР»СЏ СЂРµРєРѕРјРµРЅРґР°С†РёРё РіСЂСѓРїРїС‹."
            if msg:
                await msg.edit_text(text)
            else:
                await context.bot.send_message(telegram_id, text)
            return

    profile = get_user_profile(db_user_id)
    if profile:
        if profile.get("vo2max") and not fitness.get("vo2max"):
            source = profile.get("vo2max_source")
            updated = profile.get("vo2max_updated_at") or ""
            stale = False
            if source in ("garmin", "coros") and updated:
                try:
                    from datetime import datetime as _dt
                    stale = (_dt.now() - _dt.fromisoformat(updated)).days > 30
                except Exception:
                    stale = True
            if not stale:
                fitness["vo2max"] = profile["vo2max"]
                fitness["vo2max_source"] = source or "РїСЂРѕС„РёР»СЊ"
        if profile.get("lactate_threshold_pace"):
            fitness["lactate_threshold_pace"] = profile["lactate_threshold_pace"]
        if profile.get("lactate_threshold_hr"):
            fitness["lactate_threshold_hr"] = profile["lactate_threshold_hr"]
        if profile.get("gender"):
            fitness["gender"] = profile["gender"]

    # Р”Р°РЅРЅС‹Рµ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ вЂ” РІСЃРµРіРґР° СЃРІРµР¶РёРµ РґР»СЏ /long
    recovery = await _get_recovery_data(db_user_id, force_fresh=True)

    # РџРѕРіРѕРґР°
    weather = await get_weather_for_workout(
        workout.get("location", ""),
        workout.get("workout_date", ""),
        workout.get("schedule", ""),
    )
    weather_line = format_weather_for_message(weather) if weather else ""
    weather_prompt = format_weather_for_prompt(weather) if weather else ""

    _profile_only = fitness.get("profile_only", False)
    prefs = get_preferences(db_user_id)
    ai_mode = prefs.get("ai_mode", "smart") if prefs else "smart"
    if _profile_only:
        ai_mode = "fast"  # profile-only в†’ РїСЂРёРЅСѓРґРёС‚РµР»СЊРЅРѕ fast
    wait_msg = {"deep": "рџ§  Р”СѓРјР°СЋ РЅР°Рґ СЂРµРєРѕРјРµРЅРґР°С†РёРµР№... (~2-3 РјРёРЅСѓС‚С‹)", "smart": "вљЎ РђРЅР°Р»РёР·РёСЂСѓСЋ... (~1-2 РјРёРЅСѓС‚С‹)", "fast": "рџ”Ґ РЎС‡РёС‚Р°СЋ Р±С‹СЃС‚СЂРѕ... (~30 СЃРµРєСѓРЅРґ)"}.get(ai_mode, "вљЎ РђРЅР°Р»РёР·РёСЂСѓСЋ... (~1-2 РјРёРЅСѓС‚С‹)")
    if msg:
        await msg.edit_text(wait_msg)

    prompt = build_long_run_prompt(workout, fitness, recovery, weather_prompt=weather_prompt)
    import functools
    result = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(ask_groq, prompt, ai_mode))
    if result and result.get("timeout"):
        timeout_text = "вЏ± РњРѕРґРµР»СЊ РґСѓРјР°РµС‚ СЃР»РёС€РєРѕРј РґРѕР»РіРѕ. РџРѕРїСЂРѕР±СѓР№ вљЎ РЈРјРЅС‹Р№ СЂРµР¶РёРј (/mode)"
        if msg:
            await msg.edit_text(timeout_text, reply_markup=get_main_keyboard(from_recommendation=True))
        else:
            await context.bot.send_message(telegram_id, timeout_text)
        return
    advice = result["advice"] if result else None
    stats = result["stats"] if result else None
    garmin_json = result.get("garmin_workout") if result else None
    has_tracker = bool(
        get_token(db_user_id, "garmin") or
        get_token(db_user_id, "coros") or
        get_token(db_user_id, "polar") or
        get_token(db_user_id, "strava")
    )
    text = format_long_run_message(advice, workout, stats=stats, weather_line=weather_line, profile_only=_profile_only, has_tracker=has_tracker)

    fit_markup = None
    rating_markup = None
    if msg and advice:
        rec_group     = str(advice.get('recommended_group', ''))
        first_half    = str(advice.get('first_half_pace', ''))
        second_half   = advice.get('second_half_pace')
        _fit_data[telegram_id] = {
            'type': 'long',
            'workout': workout,
            'recommended_group': rec_group,
            'strategy': advice.get('run_strategy', 'even'),
            'first_half_pace': first_half,
            'second_half_pace': second_half,
            'garmin_json': garmin_json,
        }
        _rating_data[telegram_id] = {
            'workout_date': workout.get('workout_date', ''),
            'ai_mode': ai_mode,
        }
        fit_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("рџ“Ґ РЎРєР°С‡Р°С‚СЊ JSON", callback_data="fit_dl"),
            InlineKeyboardButton("вЊљ Р—Р°РіСЂСѓР·РёС‚СЊ РІ Garmin", callback_data="fit_up"),
        ]])
        rating_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("в­ђ РћС†РµРЅРёС‚СЊ СЂРµРєРѕРјРµРЅРґР°С†РёСЋ", callback_data="rate_show"),
        ]])

    final_markup = _merge_keyboards(fit_markup, rating_markup, get_main_keyboard(from_recommendation=True))
    if msg:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=final_markup)
    else:
        await context.bot.send_message(telegram_id, text, parse_mode="HTML",
                                       reply_markup=get_main_keyboard(from_recommendation=True))


async def _fetch_garmin_recovery(db_user_id: int) -> dict | None:
    """Р—Р°РїСЂР°С€РёРІР°РµС‚ РґР°РЅРЅС‹Рµ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ РёР· Garmin API Рё СЃРѕС…СЂР°РЅСЏРµС‚ РІ РєСЌС€."""
    from garmin import get_body_battery, get_hrv_status, get_training_readiness
    try:
        body_battery, hrv_data, readiness = await asyncio.gather(
            get_body_battery(db_user_id),
            get_hrv_status(db_user_id),
            get_training_readiness(db_user_id),
            return_exceptions=True,
        )
        result = {"source": "garmin"}
        if not isinstance(body_battery, Exception) and body_battery is not None:
            result["body_battery"] = body_battery
            result["recovery_score"] = body_battery
        if not isinstance(hrv_data, Exception) and hrv_data:
            result["hrv"] = hrv_data.get("hrv_last_night")
            result["hrv_weekly_avg"] = hrv_data.get("hrv_weekly_avg")
            result["hrv_status"] = hrv_data.get("status")
        if not isinstance(readiness, Exception) and readiness:
            result["training_readiness"] = readiness
        if len(result) > 1:
            save_garmin_recovery_cache(db_user_id, result)
            return result
    except Exception as e:
        logger.error(f"Garmin recovery fetch error for user {db_user_id}: {e}")
    return None


async def _get_recovery_data(db_user_id: int, force_fresh: bool = False) -> dict | None:
    """Р’РѕР·РІСЂР°С‰Р°РµС‚ РґР°РЅРЅС‹Рµ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ (Whoop в†’ Garmin).
    force_fresh=True вЂ” РїСЂРѕРїСѓСЃРєР°РµС‚ РєСЌС€ Garmin, РІСЃРµРіРґР° Р·Р°РїСЂР°С€РёРІР°РµС‚ API.
    РСЃРїРѕР»СЊР·СѓР№ force_fresh=True РґР»СЏ /workout Рё /long, С‡С‚РѕР±С‹ TR Рё Body Battery Р±С‹Р»Рё Р°РєС‚СѓР°Р»СЊРЅС‹РјРё.
    """
    prefs = get_preferences(db_user_id)
    use_garmin = prefs.get("use_garmin_recovery", True) if prefs else True
    has_garmin = bool(get_token(db_user_id, "garmin"))

    # Whoop вЂ” РїСЂРёРѕСЂРёС‚РµС‚
    whoop_data = None
    from whoop import get_full_recovery_data, ensure_valid_token as whoop_valid_token
    try:
        access_token = await whoop_valid_token(db_user_id)
        if access_token:
            whoop_data = await get_full_recovery_data(access_token)
    except Exception as e:
        logger.error(f"Whoop error for user {db_user_id}: {e}")

    if whoop_data:
        # Р•СЃР»Рё Garmin РЅРѕСЃСЏС‚ РїРѕСЃС‚РѕСЏРЅРЅРѕ вЂ” Training Readiness РїРѕРІРµСЂС… Whoop
        if use_garmin and has_garmin:
            try:
                tr = None
                if not force_fresh:
                    cached = get_garmin_recovery_cache(db_user_id)
                    tr = cached.get("training_readiness") if cached else None
                if not tr:
                    from garmin import get_training_readiness
                    tr = await get_training_readiness(db_user_id)
                if tr:
                    whoop_data["training_readiness"] = tr
            except Exception as e:
                logger.error(f"Garmin TR error for user {db_user_id}: {e}")
        return whoop_data

    # Garmin вЂ” РєСЌС€ (8 С‡) РёР»Рё Р¶РёРІРѕР№ Р·Р°РїСЂРѕСЃ
    if use_garmin and has_garmin:
        if not force_fresh:
            cached = get_garmin_recovery_cache(db_user_id)
            if cached:
                return cached
        garmin_result = await _fetch_garmin_recovery(db_user_id)
        if garmin_result:
            return garmin_result

    # COROS вЂ” С‚СЂРµС‚РёР№ РїСЂРёРѕСЂРёС‚РµС‚
    if get_token(db_user_id, "coros"):
        try:
            import coros as _coros
            result = await _coros.get_recovery_for_prompt(db_user_id)
            if result:
                return result
        except Exception as e:
            logger.error(f"COROS recovery error for user {db_user_id}: {e}")

    # Polar вЂ” С‡РµС‚РІС‘СЂС‚С‹Р№ РїСЂРёРѕСЂРёС‚РµС‚
    if get_token(db_user_id, "polar"):
        try:
            import polar as _polar
            result = await _polar.get_recovery_for_prompt(db_user_id)
            if result:
                return result
        except Exception as e:
            logger.error(f"Polar recovery error for user {db_user_id}: {e}")

    return None


# в”Ђв”Ђ РџР РћР’Р•Р РљРђ РќРћР’Р«РҐ РђРќРћРќРЎРћР’ в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

async def _notify_all(context, text: str, notify_key: str = "") -> int:
    """Р Р°СЃСЃС‹Р»Р°РµС‚ С‚РµРєСЃС‚ Р°РєС‚РёРІРЅС‹Рј РїРѕР»СЊР·РѕРІР°С‚РµР»СЏРј СЃ РІРєР»СЋС‡С‘РЅРЅС‹Рј СѓРІРµРґРѕРјР»РµРЅРёРµРј. Р’РѕР·РІСЂР°С‰Р°РµС‚ РєРѕР»РёС‡РµСЃС‚РІРѕ СѓСЃРїРµС€РЅС‹С…."""
    users = get_users_for_notification(notify_key) if notify_key else get_active_users()
    count = 0
    for telegram_id, name, _ in users:
        try:
            await context.bot.send_message(telegram_id, text, parse_mode="HTML")
            count += 1
            await asyncio.sleep(0.3)
        except Forbidden:
            _mark_user_inactive(telegram_id)
            logger.info(f"РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ {telegram_id} Р·Р°Р±Р»РѕРєРёСЂРѕРІР°Р» Р±РѕС‚Р°, РѕС‚РјРµС‡РµРЅ РєР°Рє РЅРµР°РєС‚РёРІРЅС‹Р№")
        except Exception as e:
            logger.error(f"Broadcast error for {telegram_id}: {e}")
    return count


async def _broadcast_split(
    context,
    text_with_data: str,
    text_no_data: str,
    notify_key: str = "",
) -> int:
    """Р Р°СЃСЃС‹Р»РєР° СЃ СЂР°Р·РЅС‹Рј С‚РµРєСЃС‚РѕРј: РїРѕР»РЅР°СЏ РІРµСЂСЃРёСЏ РґР»СЏ РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№ СЃ РґР°РЅРЅС‹РјРё, СѓРїСЂРѕС‰С‘РЅРЅР°СЏ вЂ” Р±РµР·."""
    users = get_all_users_with_status(notify_key)
    count = 0
    for telegram_id, name, _, has_data in users:
        text = text_with_data if has_data else text_no_data
        try:
            await context.bot.send_message(telegram_id, text, parse_mode="HTML")
            count += 1
            await asyncio.sleep(0.3)
        except Forbidden:
            _mark_user_inactive(telegram_id)
            logger.info(f"РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ {telegram_id} Р·Р°Р±Р»РѕРєРёСЂРѕРІР°Р» Р±РѕС‚Р°, РѕС‚РјРµС‡РµРЅ РєР°Рє РЅРµР°РєС‚РёРІРЅС‹Р№")
        except Exception as e:
            logger.error(f"Broadcast error for {telegram_id}: {e}")
    return count


def _edit_newer(a: str | None, b: str | None) -> bool:
    """True РµСЃР»Рё edit_date a РЅРѕРІРµРµ b (РѕР±Р° ISO-СЃС‚СЂРѕРєРё РёР»Рё None)."""
    if not a:
        return False
    if not b:
        return True
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(a) > _dt.fromisoformat(b)
    except Exception:
        return str(a) > str(b)


async def _autoanalyze_post(workout: dict, context=None) -> None:
    """Р¤РѕРЅРѕРІС‹Р№ Р°РІС‚РѕР°РЅР°Р»РёР· Р°РЅРѕРЅСЃР° (РЁР°Рі 1) в†’ Р·Р°РїРёСЃСЊ РІ workout_analysis.
    Р—Р°РїСѓСЃРєР°РµС‚СЃСЏ РїСЂРё: РЅРѕРІРѕРј Р°РЅРѕРЅСЃРµ / РЅРѕРІРѕР№ РґРѕРї. РіСЂСѓРїРїРµ / СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёРё РїРѕСЃС‚Р°.
    РџСЂРѕРґ-СЂРµР¶РёРј (get_preprocess_mode). РќРµ Р±Р»РѕРєРёСЂСѓРµС‚ С†РёРєР» РїСЂРѕРІРµСЂРєРё.
    РџРѕСЃР»Рµ СѓСЃРїРµС€РЅРѕРіРѕ Р°РЅР°Р»РёР·Р° СѓРІРµРґРѕРјР»СЏРµС‚ РўРћР›Р¬РљРћ Р°РґРјРёРЅР° (РєРѕРЅС‚СЂРѕР»СЊ, С‡С‚Рѕ Р±РѕС‚ РїРѕР№РјР°Р» Р°РЅРѕРЅСЃ).
    РџРѕР»СЊР·РѕРІР°С‚РµР»СЏРј РЅРёС‡РµРіРѕ РЅРµ С€Р»С‘С‚ вЂ” РёС… РµРґРёРЅСЃС‚РІРµРЅРЅРѕРµ СЃРѕРѕР±С‰РµРЅРёРµ СЌС‚Рѕ РІРµС‡РµСЂРЅСЏСЏ СЂР°СЃСЃС‹Р»РєР° 20:00.
    """
    import json as _json, functools
    try:
        post_id = workout.get("post_id")
        if not post_id:
            return
        raw_text = workout.get("raw_text") or ""
        comments_text = workout.get("comments_text") or ""
        edit_date = workout.get("edit_date")
        extra = workout.get("extra_groups") or []
        extra_json = _json.dumps(extra, ensure_ascii=False)

        existing = get_workout_analysis(post_id)
        reason = None
        if not existing:
            reason = "РЅРѕРІС‹Р№ Р°РЅРѕРЅСЃ"
        elif _edit_newer(edit_date, existing.get("edit_date")):
            reason = "РїРѕСЃС‚ РѕС‚СЂРµРґР°РєС‚РёСЂРѕРІР°РЅ"
        else:
            old_extra = _json.loads(existing.get("extra_groups_json") or "[]")
            old_nums = {str(g.get("number")) for g in old_extra}
            new_nums = {str(g.get("number")) for g in extra}
            if new_nums - old_nums:
                reason = "РЅРѕРІС‹Рµ РґРѕРї. РіСЂСѓРїРїС‹"
        if not reason:
            return

        mode = get_preprocess_mode()
        logger.info(f"autoanalyze: post_id={post_id} Р·Р°РїСѓСЃРє Р°РЅР°Р»РёР·Р° ({reason}, СЂРµР¶РёРј {mode})")
        result = await asyncio.get_event_loop().run_in_executor(
            None, functools.partial(analyze_workout, raw_text, comments_text, mode)
        )
        if not result:
            logger.warning(f"autoanalyze: post_id={post_id} Р°РЅР°Р»РёР· РЅРµ СѓРґР°Р»СЃСЏ ({reason})")
            return
        save_workout_analysis(
            post_id=post_id,
            workout_date=result.get("workout_date", ""),
            workout_type=result.get("workout_type", ""),
            is_valid=1 if result.get("is_valid") else 0,
            raw_text=raw_text,
            analyzed_json=_json.dumps(result, ensure_ascii=False),
            analysis_mode=mode,
            extra_groups_json=extra_json,
            edit_date=edit_date,
        )
        n_groups = len(result.get("groups") or [])
        n_extra = len(result.get("extra_groups") or [])
        logger.info(
            f"autoanalyze: post_id={post_id} СЃРѕС…СЂР°РЅС‘РЅ ({reason}) вЂ” "
            f"type={result.get('workout_type')}, valid={result.get('is_valid')}, "
            f"groups={n_groups}, extra={n_extra}"
        )

        # Р—Р°РїРёСЃСЊ РґР»СЏ /status (В«РїРѕСЃР»РµРґРЅРёР№ Р°РЅРѕРЅСЃВ»)
        try:
            save_workout_notification(post_id, result.get("workout_type", ""),
                                      result.get("workout_date", ""), [], 0)
        except Exception as e:
            logger.warning(f"autoanalyze: save_workout_notification error: {e}")

        # РЈРІРµРґРѕРјР»РµРЅРёРµ РўРћР›Р¬РљРћ Р°РґРјРёРЅСѓ вЂ” РєРѕРЅС‚СЂРѕР»СЊ, С‡С‚Рѕ Р°РЅРѕРЅСЃ РїРѕР№РјР°РЅ Рё СЂР°Р·РѕР±СЂР°РЅ
        if context is not None:
            valid_mark = "вњ… РІР°Р»РёРґРЅС‹Р№" if result.get("is_valid") else "вќЊ РЅРµРІР°Р»РёРґРЅС‹Р№"
            await _notify_admin(
                context.bot,
                f"рџ”¬ РђРЅРѕРЅСЃ РїРѕР№РјР°РЅ Рё РїСЂРѕР°РЅР°Р»РёР·РёСЂРѕРІР°РЅ ({reason})\n"
                f"РўРёРї: {result.get('workout_type', 'вЂ”')} | Р”Р°С‚Р°: {result.get('workout_date', 'вЂ”')}\n"
                f"{valid_mark} | РіСЂСѓРїРї: {n_groups}, РґРѕРї.РіСЂСѓРїРї: {n_extra} | СЂРµР¶РёРј {mode}"
            )
    except Exception as e:
        logger.error(f"autoanalyze error for post {workout.get('post_id')}: {e}")


async def scheduled_new_workout_check(context: ContextTypes.DEFAULT_TYPE):
    """РљР°Р¶РґС‹Рµ 30 РјРёРЅСѓС‚ Р»РѕРІРёС‚ РЅРѕРІС‹Рµ/РёР·РјРµРЅС‘РЅРЅС‹Рµ Р°РЅРѕРЅСЃС‹ Рё Р·Р°РїСѓСЃРєР°РµС‚ С„РѕРЅРѕРІС‹Р№ Р°РІС‚РѕР°РЅР°Р»РёР· (РЁР°Рі 1).
    РџРѕР»СЊР·РѕРІР°С‚РµР»СЏРј РќРР§Р•Р“Рћ РЅРµ С€Р»С‘С‚ (РЅРёРєР°РєРѕРіРѕ РїСЂРѕРјРµР¶СѓС‚РѕС‡РЅРѕРіРѕ В«РІС‹С€РµР» Р°РЅРѕРЅСЃВ») вЂ” РёС… РµРґРёРЅСЃС‚РІРµРЅРЅРѕРµ
    СЃРѕРѕР±С‰РµРЅРёРµ РїСЂРѕ С‚СЂРµРЅРёСЂРѕРІРєСѓ СЌС‚Рѕ РІРµС‡РµСЂРЅСЏСЏ СЂР°СЃСЃС‹Р»РєР° 20:00 СЃ РіРѕС‚РѕРІРѕР№ СЂРµРєРѕРјРµРЅРґР°С†РёРµР№.
    РЈРІРµРґРѕРјР»РµРЅРёРµ Рѕ РїРѕРёРјРєРµ+Р°РЅР°Р»РёР·Рµ СѓС…РѕРґРёС‚ РўРћР›Р¬РљРћ Р°РґРјРёРЅСѓ (РёР· _autoanalyze_post).
    """
    workout = await find_next_workout()
    if workout and workout.get("post_id"):
        asyncio.create_task(_autoanalyze_post(workout, context))

    workout_lr = await find_next_long_run()
    if workout_lr and workout_lr.get("post_id"):
        asyncio.create_task(_autoanalyze_post(workout_lr, context))


# в”Ђв”Ђ РџР›РђРќРР РћР’Р©РРљ в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

async def scheduled_evening(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    if now.weekday() not in [0, 3, 5]:
        return
    is_long = (now.weekday() == 5)  # СЃР± в†’ Р°РЅРѕРЅСЃ РІРѕСЃРєСЂРµСЃРЅРѕРіРѕ Long Run
    wtype = "long" if is_long else "interval"
    logger.info(f"Р—Р°РїСѓСЃРєР°СЋ РІРµС‡РµСЂРЅСЋСЋ СЂР°СЃСЃС‹Р»РєСѓ ({wtype})...")

    # РћРґРёРЅ СЂР°Р· РґРµС‚РµРєС‚РёРј СЃРІРµР¶РµСЃС‚СЊ Р°РЅРѕРЅСЃР° (find_next), РєСЌС€ вЂ” РёСЃС‚РѕС‡РЅРёРє СЂРµРєРѕРјРµРЅРґР°С†РёРё
    live = await (find_next_long_run() if is_long else find_next_workout())
    cur_post = live.get("post_id") if live else None
    cur_edit = live.get("edit_date") if live else None
    _, status = get_latest_workout_analysis(
        wtype, cur_post, live.get("workout_date") if live else None, cur_edit)
    if status == "empty":
        logger.info(f"Р’РµС‡РµСЂРЅСЏСЏ СЂР°СЃСЃС‹Р»РєР°: РЅРµС‚ Р°РЅР°Р»РёР·Р° РІ РєСЌС€Рµ ({wtype}) вЂ” РїСЂРѕРїСѓСЃРє")
        return

    users = get_all_users_with_status()
    count = 0
    for telegram_id, name, _un, _has in users:
        try:
            await _send_recommendation(telegram_id, name, context, long=is_long, live=live)
            count += 1
            await asyncio.sleep(0.5)
        except Forbidden:
            _mark_user_inactive(telegram_id)
            logger.info(f"РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ {telegram_id} Р·Р°Р±Р»РѕРєРёСЂРѕРІР°Р» Р±РѕС‚Р° (РІРµС‡РµСЂРЅСЏСЏ СЂР°СЃСЃС‹Р»РєР°)")
        except Exception as e:
            logger.error(f"Evening notification error for {telegram_id}: {e}")
    logger.info(f"Р’РµС‡РµСЂРЅСЏСЏ СЂР°СЃСЃС‹Р»РєР° Р·Р°РІРµСЂС€РµРЅР° ({wtype}, status={status}): {count} РѕС‚РїСЂР°РІР»РµРЅРѕ (РєСЌС€, Р±РµР· РїР°СЂСЃРёРЅРіР° РЅР° Р»РµС‚Сѓ)")


async def _get_vo2max_from_tracker(db_user_id: int) -> tuple:
    """РџРѕР»СѓС‡Р°РµС‚ VO2max РёР· РїРµСЂРІРѕРіРѕ РґРѕСЃС‚СѓРїРЅРѕРіРѕ С‚СЂРµРєРµСЂР° (Garmin в†’ COROS в†’ Polar).
    Р’РѕР·РІСЂР°С‰Р°РµС‚ (vo2max: float, tracker_key: str, tracker_name: str) РёР»Рё (None, None, None).
    """
    if get_token(db_user_id, "garmin"):
        try:
            from garmin import get_vo2max as _garmin_vo2max
            val = await _garmin_vo2max(db_user_id)
            if val is not None:
                return float(val), "garmin", "Garmin"
        except Exception as e:
            logger.warning(f"VO2max Garmin fetch error for uid={db_user_id}: {e}")

    if get_token(db_user_id, "coros"):
        try:
            import coros as _coros
            val = await _coros.get_vo2max(db_user_id)
            if val is not None:
                return float(val), "coros", "COROS"
        except Exception as e:
            logger.warning(f"VO2max COROS fetch error for uid={db_user_id}: {e}")

    if get_token(db_user_id, "polar"):
        try:
            import polar as _polar
            val = await _polar.get_vo2max(db_user_id)
            if val is not None:
                return float(val), "polar", "Polar"
        except Exception as e:
            logger.warning(f"VO2max Polar fetch error for uid={db_user_id}: {e}")

    return None, None, None


async def scheduled_cache_refresh(context: ContextTypes.DEFAULT_TYPE):
    """03:45 UTC (06:45 РњРЎРљ) вЂ” РѕР±РЅРѕРІР»СЏРµС‚ РєСЌС€ РІСЃРµС… СЃРµСЂРІРёСЃРѕРІ РїРµСЂРµРґ СѓС‚СЂРµРЅРЅРµР№ СЂР°СЃСЃС‹Р»РєРѕР№.
    РџРѕСЂСЏРґРѕРє: РґРѕ scheduled_morning (04:00 UTC / 07:00 РњРЎРљ).
    РћР±РЅРѕРІР»СЏРµС‚: Strava CTL/ATL/TSB, Garmin recovery+VO2max, COROS, Polar.
    """
    logger.info("Р—Р°РїСѓСЃРєР°СЋ РѕР±РЅРѕРІР»РµРЅРёРµ РєСЌС€Р° РІСЃРµС… СЃРµСЂРІРёСЃРѕРІ (03:45 UTC)...")
    users = get_all_users()
    counts = {"strava": 0, "garmin": 0, "coros": 0, "polar": 0, "vo2max": 0}

    for telegram_id, name, _ in users:
        db_user_id = get_or_create_user(telegram_id, name)

        # в”Ђв”Ђ Strava CTL/ATL/TSB в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
        try:
            access_token = await ensure_valid_token(db_user_id)
            if access_token:
                await refresh_athlete_cache(db_user_id, access_token)
                counts["strava"] += 1
        except Exception as e:
            logger.warning(f"Strava cache error for {telegram_id}: {e}")

        # в”Ђв”Ђ Garmin: recovery (Body Battery, HRV, TR) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
        if get_token(db_user_id, "garmin"):
            try:
                result = await _fetch_garmin_recovery(db_user_id)
                if result:
                    counts["garmin"] += 1
            except Exception as e:
                logger.warning(f"Garmin recovery error for {telegram_id}: {e}")

        # в”Ђв”Ђ COROS в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
        if get_token(db_user_id, "coros"):
            try:
                import coros as _coros
                await _coros.get_full_data(db_user_id)
                counts["coros"] += 1
            except Exception as e:
                logger.warning(f"COROS refresh error for {telegram_id}: {e}")

        # в”Ђв”Ђ Polar в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
        if get_token(db_user_id, "polar"):
            try:
                import polar as _polar
                await _polar.get_full_data(db_user_id)
                counts["polar"] += 1
            except Exception as e:
                logger.warning(f"Polar refresh error for {telegram_id}: {e}")

        # в”Ђв”Ђ VO2max РёР· С‚СЂРµРєРµСЂР° (С‚РёС…Рѕ) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
        new_vo2max, tracker_key, tracker_name = await _get_vo2max_from_tracker(db_user_id)
        if new_vo2max is not None:
            profile = get_user_profile(db_user_id)
            if not (profile or {}).get("vo2max_locked"):
                old_vo2max = (profile or {}).get("vo2max")
                if old_vo2max is None:
                    save_user_profile(db_user_id, vo2max=new_vo2max, vo2max_source="auto")
                elif abs(new_vo2max - float(old_vo2max)) >= 2:
                    save_user_profile(db_user_id, vo2max=new_vo2max, vo2max_source="auto")
                    counts["vo2max"] += 1
                    logger.info(
                        f"VO2max РѕР±РЅРѕРІР»С‘РЅ РґР»СЏ {telegram_id}: "
                        f"{float(old_vo2max):.0f} в†’ {new_vo2max:.0f} ({tracker_name})"
                    )

        # в”Ђв”Ђ РџРµСЂСЃРѕРЅР°Р»СЊРЅС‹Рµ С‚РµРјРїРѕРІС‹Рµ Р·РѕРЅС‹ (РїРµСЂРµСЃС‡С‘С‚ РїРѕСЃР»Рµ РѕР±РЅРѕРІР»РµРЅРёСЏ РґР°РЅРЅС‹С…) в”Ђв”Ђ
        try:
            zones.recalculate_and_save(db_user_id)
        except Exception as e:
            logger.warning(f"Zones recalc error for {telegram_id}: {e}")

        await asyncio.sleep(1)

    logger.info(
        f"РљСЌС€ РѕР±РЅРѕРІР»С‘РЅ: Strava={counts['strava']}, Garmin={counts['garmin']}, "
        f"COROS={counts['coros']}, Polar={counts['polar']}, VO2max РёР·РјРµРЅС‘РЅ={counts['vo2max']}"
    )


async def scheduled_data_refresh(context: ContextTypes.DEFAULT_TYPE):
    """Р•Р¶РµРґРЅРµРІРЅРѕ РІ 03:00 UTC РѕР±РЅРѕРІР»СЏРµС‚ Strava Рё Garmin РґР»СЏ РІСЃРµС… РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№."""
    logger.info("Р—Р°РїСѓСЃРєР°СЋ РїР»Р°РЅРѕРІРѕРµ РѕР±РЅРѕРІР»РµРЅРёРµ РґР°РЅРЅС‹С… (Strava + Garmin)...")
    users = get_all_users()
    strava_ok = garmin_ok = vo2max_ok = 0

    for telegram_id, name, _ in users:
        db_user_id = get_or_create_user(telegram_id, name)

        # Strava
        try:
            access_token = await ensure_valid_token(db_user_id)
            if access_token:
                await refresh_athlete_cache(db_user_id, access_token)
                strava_ok += 1
        except Exception as e:
            logger.error(f"Strava refresh error for {telegram_id}: {e}")

        # Garmin recovery cache
        if get_token(db_user_id, "garmin"):
            prefs = get_preferences(db_user_id)
            if prefs and prefs.get("use_garmin_recovery", True):
                result = await _fetch_garmin_recovery(db_user_id)
                if result:
                    garmin_ok += 1

            # VO2max вЂ” РѕР±РЅРѕРІР»СЏРµРј РµСЃР»Рё >7 РґРЅРµР№
            try:
                profile = get_user_profile(db_user_id)
                vo2max_updated = (profile or {}).get("vo2max_updated_at") or ""
                stale = True
                if vo2max_updated:
                    from datetime import datetime as _dt
                    stale = (_dt.now() - _dt.fromisoformat(vo2max_updated)).days > 7
                if stale:
                    from garmin import get_vo2max, get_lactate_threshold
                    vo2max, lt = await asyncio.gather(
                        get_vo2max(db_user_id),
                        get_lactate_threshold(db_user_id),
                        return_exceptions=True,
                    )
                    if not isinstance(vo2max, Exception) and vo2max:
                        save_user_profile(db_user_id, vo2max=vo2max, vo2max_source="garmin")
                        vo2max_ok += 1
                    if not isinstance(lt, Exception) and lt:
                        save_user_profile(db_user_id,
                                          lactate_threshold_pace=lt["pace"],
                                          lactate_threshold_hr=lt["hr"],
                                          lactate_source="auto")
            except Exception as e:
                logger.error(f"Garmin VO2max refresh error for {telegram_id}: {e}")

        await asyncio.sleep(1)

    logger.info(f"РћР±РЅРѕРІР»РµРЅРёРµ Р·Р°РІРµСЂС€РµРЅРѕ: Strava={strava_ok}, Garmin recovery={garmin_ok}, VO2max={vo2max_ok}")


async def scheduled_morning(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    if now.weekday() not in [1, 4, 6]:
        return
    logger.info("Р—Р°РїСѓСЃРєР°СЋ СѓС‚СЂРµРЅРЅСЋСЋ СЂР°СЃСЃС‹Р»РєСѓ...")
    # РџРѕР»СЊР·РѕРІР°С‚РµР»РµР№ Р±РµР· РїСЂРѕС„РёР»СЏ/С‚СЂРµРєРµСЂР° РЅРµ Р±РµСЃРїРѕРєРѕРёРј вЂ” РёРј РЅРµС‡РµРіРѕ РїРѕРєР°Р·С‹РІР°С‚СЊ
    users = [(tid, name, un) for tid, name, un, has in get_all_users_with_status() if has]
    for telegram_id, name, _ in users:
        try:
            await _send_morning_check(telegram_id, context)
            await asyncio.sleep(0.5)
        except Forbidden:
            _mark_user_inactive(telegram_id)
            logger.info(f"РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ {telegram_id} Р·Р°Р±Р»РѕРєРёСЂРѕРІР°Р» Р±РѕС‚Р° (СѓС‚СЂРµРЅРЅСЏСЏ СЂР°СЃСЃС‹Р»РєР°)")
        except Exception as e:
            logger.error(f"Morning notification error for {telegram_id}: {e}")


# в”Ђв”Ђ Р“Р›РћР‘РђР›Р¬РќР«Р™ РћР‘Р РђР‘РћРўР§РРљ РћРЁРР‘РћРљ в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Р›РѕРІРёС‚ РІСЃРµ РЅРµРѕР±СЂР°Р±РѕС‚Р°РЅРЅС‹Рµ РёСЃРєР»СЋС‡РµРЅРёСЏ РёР· С…РµРЅРґР»РµСЂРѕРІ."""
    error = context.error

    # РџРѕРІС‚РѕСЂРЅРѕРµ РЅР°Р¶Р°С‚РёРµ РєРЅРѕРїРєРё вЂ” СЃРѕРѕР±С‰РµРЅРёРµ СѓР¶Рµ РЅРµ РёР·РјРµРЅРёР»РѕСЃСЊ, РёРіРЅРѕСЂРёСЂСѓРµРј
    if isinstance(error, BadRequest) and "Message is not modified" in str(error):
        return

    # РўР°Р№РјР°СѓС‚С‹ Рё СЃРµС‚РµРІС‹Рµ РѕС€РёР±РєРё вЂ” РїСЂРѕСЃС‚Рѕ Р»РѕРіРёСЂСѓРµРј РЅР° СѓСЂРѕРІРЅРµ warning
    if isinstance(error, (TimedOut, NetworkError)):
        logger.warning(f"Network error: {error}")
        return

    # Р’СЃС‘ РѕСЃС‚Р°Р»СЊРЅРѕРµ вЂ” Р»РѕРіРёСЂСѓРµРј РїРѕР»РЅРѕСЃС‚СЊСЋ
    logger.error("РќРµРѕР±СЂР°Р±РѕС‚Р°РЅРЅРѕРµ РёСЃРєР»СЋС‡РµРЅРёРµ:", exc_info=error)

    # РЈРІРµРґРѕРјР»СЏРµРј РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ
    if update and hasattr(update, 'effective_chat') and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "вљ пёЏ Р§С‚Рѕ-С‚Рѕ РїРѕС€Р»Рѕ РЅРµ С‚Р°Рє. РџРѕРїСЂРѕР±СѓР№ РµС‰С‘ СЂР°Р· РёР»Рё РїРµСЂРµРєР»СЋС‡Рё СЂРµР¶РёРј AI (/mode)"
            )
        except Exception:
            pass


# в”Ђв”Ђ Р—РђРџРЈРЎРљ в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("workout", cmd_workout))
    app.add_handler(CommandHandler("long", cmd_long))
    app.add_handler(CommandHandler("morning", cmd_morning))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("connect_strava", cmd_connect_strava))
    app.add_handler(CommandHandler("connect_whoop", cmd_connect_whoop))
    app.add_handler(CommandHandler("connect_garmin", cmd_connect_garmin))
    app.add_handler(CommandHandler("connect_coros", cmd_connect_coros))
    app.add_handler(CommandHandler("connect_polar", cmd_connect_polar))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("users",    cmd_users))
    app.add_handler(CommandHandler("services", cmd_services))
    app.add_handler(CommandHandler("prompt",   cmd_prompt))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("debug_long", cmd_debug_long))
    app.add_handler(CommandHandler("notifications", cmd_notifications))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("feedback",  cmd_feedback))
    app.add_handler(CommandHandler("ratings",   cmd_ratings))
    app.add_handler(CommandHandler("feedbacks", cmd_feedbacks))
    app.add_handler(CommandHandler("analyze",   cmd_analyze))
    app.add_handler(CommandHandler("preprocess_mode", cmd_preprocess_mode))
    app.add_handler(CommandHandler("test_workout", cmd_test_workout))
    app.add_handler(CommandHandler("test_long",    cmd_test_long))
    app.add_handler(CommandHandler("reanalyze",    cmd_reanalyze))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(global_error_handler)

    job_queue = app.job_queue
    job_queue.run_daily(scheduled_evening,       time=time(hour=17, minute=0))           # 20:00 РњРЎРљ
    job_queue.run_daily(scheduled_cache_refresh, time=time(hour=3,  minute=45))          # 06:45 РњРЎРљ вЂ” РІСЃРµ СЃРµСЂРІРёСЃС‹
    job_queue.run_daily(scheduled_morning,       time=time(hour=4,  minute=0))           # 07:00 РњРЎРљ вЂ” РїРѕСЃР»Рµ РєСЌС€Р°
    job_queue.run_repeating(scheduled_new_workout_check, interval=1800, first=60)        # РєР°Р¶РґС‹Рµ 30 РјРёРЅ

    import oauth_server as _oauth
    _oauth.set_telegram_app(app)

    async def _run():
        import signal
        from aiohttp import web as _aio_web

        # Start OAuth callback server on port 8080
        runner = _aio_web.AppRunner(_oauth.create_web_app())
        await runner.setup()
        site = _aio_web.TCPSite(runner, "0.0.0.0", 8080)
        await site.start()
        logger.info("OAuth server started on :8080")

        # Р•РґРёРЅС‹Р№ Telethon-РєР»РёРµРЅС‚ РЅР° РїСЂРѕС†РµСЃСЃ (РїСЂРѕРіСЂРµРІ)
        try:
            import telegram_reader as _tr
            await _tr.connect_client()
        except Exception as e:
            logger.warning(f"Telethon warmup failed: {e}")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass  # Windows

        try:
            async with app:
                await app.updater.start_polling()
                await app.start()
                logger.info("вњ… Р‘РѕС‚ Р·Р°РїСѓС‰РµРЅ!")
                try:
                    await stop_event.wait()
                finally:
                    await app.updater.stop()
                    await app.stop()
                logger.info("Р‘РѕС‚ РѕСЃС‚Р°РЅРѕРІР»РµРЅ")
        finally:
            await runner.cleanup()
            logger.info("OAuth server stopped")
            try:
                import telegram_reader as _tr
                await _tr.close_client()
                logger.info("Telethon client closed")
            except Exception:
                pass

    asyncio.run(_run())


if __name__ == "__main__":
    main()
