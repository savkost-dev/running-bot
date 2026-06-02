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
    """Помечает пользователя как неактивного (заблокировал бота)."""
    try:
        db_user_id = get_or_create_user(telegram_id, "")
        set_preference(db_user_id, "is_active", 0)
        set_preference(db_user_id, "deactivated_at", datetime.now().isoformat())
    except Exception as e:
        logger.error(f"Failed to mark user {telegram_id} inactive: {e}")


def _mark_user_active_if_needed(telegram_id: int, name: str = "", username: str = None) -> int:
    """Восстанавливает is_active=1 при входящем сообщении. Возвращает db_user_id."""
    db_user_id = get_or_create_user(telegram_id, name, username)
    prefs = get_preferences(db_user_id)
    if prefs and not prefs.get("is_active", True):
        set_preference(db_user_id, "is_active", 1)
        set_preference(db_user_id, "deactivated_at", None)
        logger.info(f"Пользователь {telegram_id} снова активен")
    return db_user_id


async def _notify_admin(bot, text: str) -> None:
    """Отправляет уведомление администратору. Тихо игнорирует ошибки."""
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as e:
        logger.warning(f"Admin notify failed: {e}")


last_workout: dict | None = None
last_long_run: dict | None = None

# In-memory cache: telegram_id → FIT generation params (set after recommendation)
_fit_data: dict[int, dict] = {}
# In-memory cache: telegram_id → rating context (workout_date, ai_mode)
_rating_data: dict[int, dict] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


# ── КЭШ АТЛЕТА ───────────────────────────────────────────────

async def refresh_athlete_cache(db_user_id: int, access_token: str, notify_msg=None) -> dict | None:
    """
    Обновляет кэш данных атлета (CTL/ATL/TSB, прогнозы, соревнования).
    Занимает 30-60 сек — вызывать только при подключении или по запросу.
    """
    try:
        if notify_msg:
            await notify_msg.edit_text(
                "⏳ Загружаю данные из Strava...\n"
                "Это займёт около минуты (только первый раз)"
            )

        athlete_data = await get_full_athlete_data(access_token)

        save_athlete_cache(
            db_user_id,
            athlete_data["training_load"],
            athlete_data["predictions"],
            athlete_data["last_race"]
        )

        logger.info(f"Кэш атлета обновлён для user_id={db_user_id}")
        return athlete_data

    except Exception as e:
        logger.error(f"Ошибка обновления кэша для user_id={db_user_id}: {e}")
        return None


async def get_fitness_data(db_user_id: int, access_token: str) -> dict | None:
    """
    Получает данные атлета:
    - Быстрые (всегда свежие): пробежки за 14 дней + острая нагрузка за 48 ч
    - Медленные (из кэша): CTL/ATL/TSB, прогнозы Риегеля, последнее соревнование
    """
    from strava import get_recent_runs, analyze_fitness, get_recent_48h_load

    # ── Быстрые данные (2 запроса к Strava API) ──────────────
    try:
        runs, load_48h = await asyncio.gather(
            get_recent_runs(access_token, days=14),
            get_recent_48h_load(access_token),
        )
        fitness = analyze_fitness(runs)
        fitness["load_48h"] = load_48h
    except Exception as e:
        logger.error(f"Ошибка получения пробежек: {e}")
        fitness = {"summary": "Нет данных", "total_km": 0, "run_count": 0,
                   "avg_pace": "—", "avg_hr": None, "fatigue_level": "unknown",
                   "load_48h": None}

    # ── Медленные данные (из кэша) ────────────────────────────
    cache = get_athlete_cache(db_user_id)
    if cache:
        fitness["training_load"] = cache["training_load"]
        fitness["predictions"]   = cache["predictions"]
        fitness["last_race"]     = cache["last_race"]
    else:
        logger.info(f"Кэш отсутствует для user_id={db_user_id}, обновляю...")
        athlete_data = await refresh_athlete_cache(db_user_id, access_token)
        if athlete_data:
            fitness["training_load"] = athlete_data["training_load"]
            fitness["predictions"]   = athlete_data["predictions"]
            fitness["last_race"]     = athlete_data["last_race"]

    return fitness


async def get_garmin_fitness_data(db_user_id: int) -> dict | None:
    """
    Получает данные атлета из Garmin Connect — аналог get_fitness_data() для Strava.
    Возвращает dict, совместимый со структурой fitness для промта.
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
        "avg_pace": "—",
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
    Получает данные атлета из COROS — аналог get_garmin_fitness_data().
    Возвращает dict, совместимый со структурой fitness для промта.
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
    Получает данные атлета из Polar — аналог get_coros_fitness_data().
    Возвращает dict, совместимый со структурой fitness для промта.
    """
    import polar as _polar
    if not get_token(db_user_id, "polar"):
        return None
    try:
        return await _polar.get_full_data(db_user_id)
    except Exception as e:
        logger.error(f"Polar fitness data error for {db_user_id}: {e}")
        return None


# ── НАВИГАЦИЯ ─────────────────────────────────────────────────

def get_main_keyboard(from_recommendation: bool = False) -> InlineKeyboardMarkup:
    """Краткое меню под каждым ответом.
    from_recommendation=True → главное меню открывается новым сообщением (/start-поведение).
    from_recommendation=False → редактирует текущее сообщение (навигационные экраны).
    """
    home_data = "main_menu_new" if from_recommendation else "main_menu"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Тренировка", callback_data="get_workout"),
         InlineKeyboardButton("🕐 Long Run",   callback_data="get_long_run")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data=home_data)],
    ])


def _merge_keyboards(*keyboards) -> InlineKeyboardMarkup:
    """Объединяет несколько InlineKeyboardMarkup в один."""
    rows = []
    for kb in keyboards:
        if kb:
            rows.extend(kb.inline_keyboard)
    return InlineKeyboardMarkup(rows)


def _build_screen1_keyboard() -> InlineKeyboardMarkup:
    """Экран 1 /start — основные действия."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Тренировка", callback_data="get_workout"),
         InlineKeyboardButton("🕐 Long Run",   callback_data="get_long_run")],
        [InlineKeyboardButton("☀️ Утро",       callback_data="get_morning"),
         InlineKeyboardButton("👤 Профиль",   callback_data="my_profile")],
        [InlineKeyboardButton("💬 Обратная связь", callback_data="feedback_show"),
         InlineKeyboardButton("❓ Справка",        callback_data="help")],
        [InlineKeyboardButton("⚙️ Настройки →",   callback_data="show_settings")],
    ])


def _build_screen1_onboarding_keyboard() -> InlineKeyboardMarkup:
    """Экран 1 — онбординг нового пользователя (нет профиля или трекера)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 1. Заполнить профиль",  callback_data="my_profile")],
        [InlineKeyboardButton("🔗 2. Подключить трекер",  callback_data="show_services")],
        [InlineKeyboardButton("📋 Тренировка", callback_data="get_workout"),
         InlineKeyboardButton("🕐 Long Run",   callback_data="get_long_run")],
    ])


def _build_screen2_keyboard() -> InlineKeyboardMarkup:
    """Экран 2 — настройки."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Режим AI",        callback_data="ai_mode"),
         InlineKeyboardButton("🔔 Уведомления",    callback_data="notifications")],
        [InlineKeyboardButton("🔄 Обновить данные", callback_data="refresh_cache"),
         InlineKeyboardButton("🔗 Сервисы →",       callback_data="show_services")],
        [InlineKeyboardButton("← Назад",            callback_data="main_menu")],
    ])


def _settings_nav() -> list:
    """Строка навигации: ← Настройки + 🏠 Главное меню."""
    return [
        InlineKeyboardButton("← Настройки", callback_data="settings_menu"),
        InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"),
    ]


# Метаданные сервисов: (ключ, эмодзи, название, connect_callback, disconnect_done_msg)
_SERVICES = [
    ("strava", "🟠", "Strava",  "connect_strava",      "Strava отключена"),
    ("whoop",  "⚪", "Whoop",   "connect_whoop_btn",   "Whoop отключён"),
    ("garmin", "🔵", "Garmin",  "connect_garmin_btn",  "Garmin отключён"),
    ("coros",  "🔴", "COROS",   "connect_coros_btn",   "COROS отключён"),
    ("polar",  "❄️", "Polar",   "connect_polar_btn",   "Polar отключён"),
]


def _svc_name(svc: str) -> str:
    """Возвращает отображаемое имя сервиса по ключу."""
    return next((name for s, _, name, _, _ in _SERVICES if s == svc), svc)


def _svc_done_msg(svc: str) -> str:
    """Возвращает сообщение после отключения сервиса."""
    return next((msg for s, _, _, _, msg in _SERVICES if s == svc), f"{svc} отключён")


def _build_screen3_keyboard(db_user_id: int) -> InlineKeyboardMarkup:
    """Экран 3 — подключение/отключение сервисов.

    Подключён:    [🟠 Strava ✅]  [❌ Отключить]
    Не подключён: [🟠 Strava ❌  Подключить]
    """
    rows = []
    for svc, emoji, name, connect_cb, _ in _SERVICES:
        if get_token(db_user_id, svc):
            rows.append([
                InlineKeyboardButton(f"{emoji} {name} ✅", callback_data="svc_noop"),
                InlineKeyboardButton("❌ Отключить",        callback_data=f"disc_ask_{svc}"),
            ])
        else:
            if svc == "strava":
                label = f"{emoji} {name} ❌  (на проверке)"
            else:
                label = f"{emoji} {name} ❌  Подключить"
            rows.append([InlineKeyboardButton(label, callback_data=connect_cb)])
    rows.append(_settings_nav())
    return InlineKeyboardMarkup(rows)


def _build_main_menu_content(user, db_user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Строит (text, keyboard) для главного меню — используется и при edit, и при send."""
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
        status_lines = ["✅ Профиль заполнен"]
        if strava:
            status_lines.append("✅ Strava подключена")
        if garmin:
            status_lines.append("✅ Garmin подключён")
        if whoop:
            status_lines.append("✅ Whoop подключён")

        fitness_src   = "CTL/ATL/TSB (Strava)" if strava else "Training Load (Garmin)"
        recovery_name = "Whoop" if whoop else "Garmin"

        text = (
            f"Привет, {user.first_name}! 👋\n\n"
            + "\n".join(status_lines) + "\n\n"
            "Что умею:\n"
            f"🏃 Анализирую форму ({fitness_src}), восстановление и рекомендую группу "
            "для тренировки вт/пт с процентной шкалой подходимости\n"
            "🕐 То же самое для воскресного Long Run с рекомендацией стратегии "
            "(ровный темп или прогрессия)\n"
            f"☀️ Утром в день тренировки проверяю восстановление и корректирую план\n"
            "📢 Автоматически уведомляю когда выходит новый анонс тренировки\n\n"
            "Выбери действие 👇"
        )
    else:
        # Показываем чек-лист того, что уже подключено
        done = []
        if profile_ok:
            done.append("✅ Профиль заполнен")
        if fitness_ok:
            svc_names = []
            if garmin: svc_names.append("Garmin")
            if coros:  svc_names.append("COROS")
            if polar:  svc_names.append("Polar")
            if strava: svc_names.append("Strava")
            done.append("✅ Трекер: " + ", ".join(svc_names))
        if whoop:
            done.append("✅ Whoop подключён")

        done_block = ("\n" + "\n".join(done) + "\n") if done else ""

        text = (
            f"Привет, {user.first_name}! 👋\n"
            f"{done_block}\n"
            "Я помогу подготовиться к тренировкам Dusty Dumbbells.\n\n"
            "Для начала сделай два шага:\n"
            "1️⃣ Заполни профиль — VO2max и лактатный порог\n"
            "2️⃣ Подключи трекер — Garmin, COROS, Polar или Strava\n\n"
            "Выбери действие 👇"
        )
        return text, _build_screen1_onboarding_keyboard()

    return text, _build_screen1_keyboard()


async def _show_main_menu(query_or_update, user, db_user_id: int):
    """Показывает Экран 1 редактируя текущее сообщение (для навигационных экранов)."""
    text, keyboard = _build_main_menu_content(user, db_user_id)
    if hasattr(query_or_update, 'edit_message_text'):
        try:
            await query_or_update.edit_message_text(text, reply_markup=keyboard)
        except Exception:
            pass
    else:
        await query_or_update.message.reply_text(text, reply_markup=keyboard)


# ── КОМАНДЫ ───────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = not user_exists(user.id)
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    if is_new:
        total = len(get_all_users())
        uname = f" (@{user.username})" if user.username else ""
        await _notify_admin(
            context.bot,
            f"👤 Новый пользователь: {user.full_name}{uname}\n"
            f"Всего пользователей: {total}"
        )
    await _show_main_menu(update, user, db_user_id)


async def cmd_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    log_activity(db_user_id, '/workout')
    msg = await update.message.reply_text("🔍 Ищу анонс, анализирую и подбираю группу...")
    await _send_recommendation(user.id, user.full_name, context, long=False, msg=msg)


async def cmd_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    log_activity(db_user_id, '/long')
    msg = await update.message.reply_text("🔍 Подбираю Long Run...")
    await _send_recommendation(user.id, user.full_name, context, long=True, msg=msg)


async def cmd_morning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    log_activity(db_user_id, '/morning')
    msg = await update.message.reply_text("☀️ Проверяю твоё восстановление...")
    await _send_morning_check(user.id, context, msg)


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительное обновление кэша данных атлета"""
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)

    access_token = await ensure_valid_token(db_user_id)
    if not access_token:
        await update.message.reply_text("❌ Strava не подключена. Сначала /connect_strava")
        return

    msg = await update.message.reply_text("⏳ Обновляю данные...")
    athlete_data = await refresh_athlete_cache(db_user_id, access_token, msg)

    if athlete_data:
        load = athlete_data["training_load"]
        cache = get_athlete_cache(db_user_id)
        updated_at = cache["updated_at"] if cache else "только что"
        await msg.edit_text(
            f"✅ Данные обновлены!\n\n"
            f"Тренированность (CTL): {load.get('ctl', '—')}\n"
            f"Усталость (ATL): {load.get('atl', '—')}\n"
            f"Форма (TSB): {load.get('tsb', '—')} — {load.get('form_text', '—')}\n"
            f"Тренд: {load.get('trend_text', '—')}\n\n"
            f"Обновлено: {updated_at}"
        )
    else:
        await msg.edit_text("❌ Не удалось обновить данные. Попробуй позже.")


def _fmt_workout_date(workout_date: str) -> tuple[str, str]:
    """Возвращает (date_fmt '27.05', weekday 'Вторник')."""
    _WEEKDAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    try:
        from datetime import datetime as _dt
        dt_obj = _dt.strptime(workout_date, "%Y-%m-%d")
        return dt_obj.strftime("%d.%m"), _WEEKDAYS_RU[dt_obj.weekday()]
    except Exception:
        return workout_date, ""


def _build_simple_workout_text(workout: dict) -> str:
    """Упрощённое уведомление для пользователей без профиля/трекера."""
    date_fmt, weekday = _fmt_workout_date(workout.get("workout_date", ""))
    location  = workout.get("location") or "—"
    schedule  = workout.get("schedule") or "—"
    work_text = (workout.get("work_text") or "").strip()
    groups_raw = (workout.get("groups_raw") or "").strip()

    lines = [f"📢 Завтра тренировка Dusty Dumbbells!\n"]
    lines.append(f"{weekday} {date_fmt} | 📍 {location}")
    lines.append(f"⏰ {schedule}")
    if work_text:
        lines.append(f"\n💪 {work_text}")
    if groups_raw:
        lines.append(f"\nГруппы:\n{groups_raw[:400]}")
    lines.append(
        "\nМне очень жаль, что могу только напомнить тебе о тренировке, "
        "но не могу дать рекомендаций о погоде, разминке, группе, питании и стратегии. 🤷"
    )
    lines.append(
        "\nЧтобы получить полный анализ — заполни профиль и подключи трекер:\n"
        "👤 /profile — VO2max и лактатный порог\n"
        "🔗 Garmin, COROS или Polar — /connect_garmin, /connect_coros"
    )
    return "\n".join(lines)


def _build_status_text(db_user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    strava = get_token(db_user_id, "strava")
    whoop = get_token(db_user_id, "whoop")
    garmin = get_token(db_user_id, "garmin")
    cache = get_athlete_cache(db_user_id)
    prefs = get_preferences(db_user_id)
    use_garmin_rec = prefs.get("use_garmin_recovery", True) if prefs else True

    lines = ["Подключённые сервисы:\n"]
    lines.append(f"{'✅' if strava else '❌'} Strava")
    lines.append(f"{'✅' if whoop else '❌'} Whoop")
    lines.append(f"{'✅' if garmin else '❌'} Garmin")

    if cache:
        lines.append(f"\nДанные Strava: обновлены {cache['updated_at'][:10]}")
    else:
        lines.append("\nДанные Strava: не загружены (/refresh)")

    # Источник данных восстановления
    lines.append("\nИсточник восстановления:")
    if whoop:
        lines.append("  Whoop (приоритет)")
        if garmin:
            lines.append("  Garmin — резерв если Whoop недоступен")
    elif garmin:
        if use_garmin_rec:
            lines.append("  Garmin (Body Battery, HRV)")
        else:
            lines.append("  Garmin подключён, но данные восстановления отключены")
    else:
        lines.append("  Нет данных (подключи Whoop или Garmin)")

    keyboard = None
    if garmin and not whoop:
        toggle_label = "Отключить Garmin для восстановления" if use_garmin_rec else "Включить Garmin для восстановления"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(toggle_label, callback_data="toggle_garmin_recovery")
        ]])

    if not strava:
        lines.append("\nПодключи Strava: /connect_strava")

    last_notif = get_last_workout_notification()
    if last_notif and last_notif.get("workout_date"):
        date_fmt, weekday = _fmt_workout_date(last_notif["workout_date"])
        lines.append(f"\nПоследний анонс: {weekday} {date_fmt} (уведомлено {last_notif['users_notified']} польз.)")

    lines.append(f"\nВерсия бота: {VERSION} ({BUILD_DATE})")

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
    keyboard = [[InlineKeyboardButton("🔗 Войти в Strava", url=auth_url)]]
    await update.message.reply_text(
        "⚠️ Strava временно ограничена — подключение новых пользователей на проверке у Strava.\n"
        "Используй Garmin или COROS (/connect_garmin, /connect_coros) для полноценной работы.\n\n"
        "Нажми кнопку и авторизуйся в Strava.\n\n"
        "После авторизации ты автоматически получишь сообщение в Telegram — ничего копировать не нужно.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_connect_whoop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from whoop import get_auth_url as whoop_auth_url
    user = update.effective_user
    _mark_user_active_if_needed(user.id, user.full_name, user.username)
    auth_url = whoop_auth_url(user.id)
    keyboard = [[InlineKeyboardButton("🔗 Войти в Whoop", url=auth_url)]]
    await update.message.reply_text(
        "Нажми кнопку и авторизуйся в Whoop.\n\n"
        "После авторизации браузер откроет страницу с JSON — "
        "скопируй весь URL из адресной строки и отправь мне сюда.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["awaiting_whoop_code"] = True


async def cmd_connect_garmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _mark_user_active_if_needed(update.effective_user.id, update.effective_user.full_name, update.effective_user.username)
    await update.message.reply_text(
        "Подключение Garmin Connect\n\n"
        "Email и пароль хранятся на сервере в зашифрованном виде (AES-256) — "
        "в открытом виде они нигде не сохраняются.\n\n"
        "Введи email от Garmin Connect:"
    )
    context.user_data["awaiting_garmin"] = "email"


async def cmd_connect_coros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _mark_user_active_if_needed(update.effective_user.id, update.effective_user.full_name, update.effective_user.username)
    await update.message.reply_text(
        "Подключение COROS\n\n"
        "Email и пароль хранятся на сервере в зашифрованном виде (AES-256) — "
        "в открытом виде они нигде не сохраняются.\n\n"
        "Введи email от аккаунта COROS:"
    )
    context.user_data["awaiting_coros"] = "email"


async def cmd_connect_polar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from polar import get_auth_url as polar_auth_url
    user = update.effective_user
    _mark_user_active_if_needed(user.id, user.full_name, user.username)
    auth_url = polar_auth_url(user.id)
    if not auth_url:
        await update.message.reply_text(
            "❌ Polar не настроен. Обратитесь к администратору."
        )
        return
    keyboard = [[InlineKeyboardButton("🔗 Войти в Polar", url=auth_url)]]
    await update.message.reply_text(
        "Подключение Polar\n\n"
        "Нажми кнопку и авторизуйся в Polar Flow.\n\n"
        "После авторизации ты автоматически получишь сообщение в Telegram — ничего копировать не нужно.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает последний промпт (только для админов)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    prompt = claude_advisor.last_prompt
    if not prompt:
        await update.message.reply_text("Промпт ещё не отправлялся. Сначала /workout.")
        return
    text = f"Последний промпт ({len(prompt)} симв.):\n\n{prompt}"
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
            return f"Garmin · {date_str} · устарело"
        return f"Garmin · {date_str}"
    if source == "manual":
        return "вручную"
    return "вручную" if updated else ""


SPECIALIZATIONS = {
    "5k": "5 км",
    "10k": "10 км",
    "half_marathon": "Полумарафон",
    "marathon": "Марафон",
    "speed": "Развитие скорости",
    "fitness": "Общая форма",
}


def _build_profile_text(profile: dict | None) -> str:
    if not profile or not any([profile.get("vo2max"), profile.get("lactate_threshold_pace"), profile.get("gender")]):
        return "Профиль не заполнен. Используй кнопки ниже чтобы добавить данные."
    lines = ["Твой профиль:\n"]
    if profile.get("gender"):
        lines.append(f"Пол: {'Мужской' if profile['gender'] == 'male' else 'Женский'}")
    if profile.get("vo2max"):
        tag = _vo2max_tag(profile)
        lock_icon = " 🔒" if profile.get("vo2max_locked") else ""
        lines.append(f"VO2max: {profile['vo2max']} мл/кг/мин{f'  ({tag})' if tag else ''}{lock_icon}")
    if profile.get("lactate_threshold_pace"):
        lt = f"Лактатный порог: {profile['lactate_threshold_pace']} мин/км"
        if profile.get("lactate_threshold_hr"):
            lt += f" при ЧСС {profile['lactate_threshold_hr']} уд/мин"
        lt_source = profile.get("lactate_source")
        lt_lock = " 🔒" if profile.get("lactate_locked") else ""
        if lt_source:
            lt += f"  ({'вручную' if lt_source == 'manual' else 'из сервиса'}){lt_lock}"
        elif lt_lock:
            lt += f"  {lt_lock.strip()}"
        lines.append(lt)
    spec = profile.get("specialization")
    spec_label = SPECIALIZATIONS.get(spec) if spec else None
    lines.append(f"Специализация: {spec_label or 'Полумарафон (по умолчанию)'}")
    if profile.get("updated_at"):
        lines.append(f"\nОбновлено: {profile['updated_at'][:10]}")
    return '\n'.join(lines)


def _build_profile_keyboard(profile: dict | None = None) -> InlineKeyboardMarkup:
    p = profile or {}
    rows = [
        [InlineKeyboardButton("👤 Пол", callback_data="profile_set_gender"),
         InlineKeyboardButton("🎯 Специализация", callback_data="profile_set_specialization")],
        [InlineKeyboardButton("📊 Указать VO2max",   callback_data="profile_set_vo2max"),
         InlineKeyboardButton("🏃 Лактатный порог", callback_data="profile_set_lactate")],
    ]
    lock_row = []
    if p.get("vo2max") is not None:
        lbl = "🔒 VO2max заблокирован" if p.get("vo2max_locked") else "🔓 VO2max (обновлять)"
        lock_row.append(InlineKeyboardButton(lbl, callback_data="profile_toggle_vo2max_lock"))
    if p.get("lactate_threshold_pace"):
        lbl = "🔒 ЛП заблокирован" if p.get("lactate_locked") else "🔓 ЛП (обновлять)"
        lock_row.append(InlineKeyboardButton(lbl, callback_data="profile_toggle_lactate_lock"))
    if lock_row:
        rows.append(lock_row)
    rows.append(_settings_nav())
    return InlineKeyboardMarkup(rows)


def _build_specialization_keyboard(current_spec: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"{'✅ ' if key == current_spec else ''}{label}",
            callback_data=f"spec_set_{key}")]
        for key, label in SPECIALIZATIONS.items()
    ]
    rows.append([InlineKeyboardButton("← Назад", callback_data="my_profile")])
    return InlineKeyboardMarkup(rows)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    profile = get_user_profile(db_user_id)
    await update.message.reply_text(
        _build_profile_text(profile),
        reply_markup=_build_profile_keyboard(profile)
    )


# Режим формирования РЕКОМЕНДАЦИИ (Шаг 2). Анализ анонса (Шаг 1) всегда deep (админ).
_MODE_INFO = {
    "deep":  ("🧠", "Глубокий (ИИ)", "~2 мин",     "ИИ формулирует рекомендацию, макс. качество"),
    "smart": ("⚡", "Быстрый (ИИ)",  "~30-60 сек", "баланс качества и скорости"),
    "fast":  ("🪶", "Лёгкий (ИИ)",   "~10 сек",    "короткое ИИ-объяснение"),
    "calc":  ("📊", "Расчётный",      "формулы",    "группа и % по формулам, текст коротко от ИИ"),
}


def _build_mode_text(current_mode: str) -> str:
    lines = ["🧠 Режим рекомендации (как бот формулирует совет по группе):\n"]
    for key, (emoji, label, timing, desc) in _MODE_INFO.items():
        mark = "✅ " if key == current_mode else "   "
        lines.append(f"{mark}{emoji} {label} ({timing}) — {desc}")
    lines.append("\nЧисла (группа, %, зоны) всегда считаются формулами; режим влияет на то,\n"
                 "насколько глубоко ИИ объясняет и формулирует текст. Выбери режим:")
    return "\n".join(lines)


def _build_mode_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    def btn(key):
        emoji, label, _timing, _ = _MODE_INFO[key]
        mark = "✅ " if key == current_mode else ""
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
    def mark(key): return "✅" if (prefs or {}).get(key, True) else "❌"
    return (
        "🔔 Настройки уведомлений:\n\n"
        f"{mark('notify_interval')} Тренировки вт/пт\n"
        f"{mark('notify_interval_extra')} Новые группы\n"
        f"{mark('notify_long')} Воскресный Long Run"
    )


def _build_notifications_keyboard(prefs: dict) -> InlineKeyboardMarkup:
    def lbl(key, title):
        on = (prefs or {}).get(key, True)
        action = f"notif_off_{key}" if on else f"notif_on_{key}"
        return InlineKeyboardButton(f"{'✅' if on else '❌'} {title} — {'[Выкл]' if on else '[Вкл]'}", callback_data=action)
    return InlineKeyboardMarkup([
        [lbl("notify_interval", "Тренировки вт/пт")],
        [lbl("notify_interval_extra", "Новые группы")],
        [lbl("notify_long", "Воскресный Long Run")],
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
        "Команды:\n"
        "/start — главное меню\n"
        "/workout — рекомендация группы для вт/пт тренировки\n"
        "/long — рекомендация для воскресного Long Run\n"
        "/morning — утренняя проверка восстановления\n"
        "/status — статус подключённых сервисов\n"
        "/refresh — обновить данные из Strava\n"
        "/profile — профиль (VO2max, лактатный порог)\n"
        "/mode — режим ИИ (быстрый / глубокий)\n"
        "/notifications — настройки уведомлений\n"
        "/connect_strava — подключить Strava\n"
        "/connect_garmin — подключить Garmin Connect\n"
        "/connect_whoop — подключить Whoop\n"
        "/connect_coros — подключить COROS\n"
        "/connect_polar — подключить Polar\n"
        "/feedback — обратная связь (проблема / идея)\n"
        "/help — эта справка\n\n"
        "Автоматические уведомления:\n"
        "• Накануне тренировки (пн, чт, сб) в 20:00 МСК\n"
        "• Утром в день тренировки в 07:00 МСК"
    )
    if is_admin:
        text += (
            "\n\n— Администратор —\n"
            "/stats — статистика пользователей и активности\n"
            "/users — список всех пользователей\n"
            "/services — пользователи по подключённым сервисам\n"
            "/prompt — последний промпт к модели\n"
            "/debug — разбор последней тренировки\n"
            "/debug_long — разбор последнего Long Run\n"
            "/ratings — последние оценки рекомендаций\n"
            "/feedbacks — последние сообщения обратной связи\n"
            "/analyze — анализ последней тренировки через DeepSeek\n"
            "/preprocess_mode — режим анализа тренировок (deep/smart)\n"
            "/test_workout — тест Шага 2 (рекомендация группы) на твоих данных\n"
            "/test_long — тест Шага 2 для длительной на твоих данных\n"
            "/reanalyze — форс переанализа свежих анонсов (обновить кэш)\n"
            "/show_analyze — показать последний Шаг 1 из базы"
        )
    return text


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _mark_user_active_if_needed(user.id, user.full_name, user.username)
    is_admin = user.id in ADMIN_TELEGRAM_IDS
    await update.message.reply_text(_build_help_text(is_admin), reply_markup=get_main_keyboard())


def _build_feedback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🐛 Проблема",  callback_data="feedback_bug"),
         InlineKeyboardButton("💡 Идея",      callback_data="feedback_feature")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _mark_user_active_if_needed(user.id, user.full_name, user.username)
    await update.message.reply_text("Выбери тип:", reply_markup=_build_feedback_keyboard())


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика бота (только для админов)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    s = get_bot_stats()
    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Пользователи: {s['total']}\n"
        f"✅ Активных: {s.get('active_bot', s['total'])}\n"
        f"💤 Неактивных: {s.get('inactive_bot', 0)}\n"
        f"Новых за 7 дней: {s['new_7d']}\n"
        f"Активных за 7 дней: {s['active_7d']}\n\n"
        "Подключения:\n"
        f"🟠 Strava: {s['strava']}\n"
        f"⚪ Whoop: {s['whoop']}\n"
        f"🔵 Garmin: {s['garmin']}\n"
        f"🔴 COROS: {s['coros']}\n"
        f"❄️ Polar: {s.get('polar', 0)}\n"
        f"👤 Профиль заполнен: {s['profile']}\n\n"
        "Запросы за 7 дней:\n"
        f"📋 /workout: {s['workout_7d']}\n"
        f"🕐 /long: {s['long_7d']}\n"
        f"☀️ /morning: {s['morning_7d']}\n\n"
        f"⭐ Средняя оценка: {s.get('avg_rating') or '—'}/10 (за 30 дней)\n"
        f"📊 Оценок получено: {s.get('ratings_30d', 0)}\n"
        f"💬 Обратной связи: {s.get('feedback_total', 0)} "
        f"(баги: {s.get('feedback_bugs', 0)}, идеи: {s.get('feedback_features', 0)})"
    )
    await update.message.reply_text(text, parse_mode="HTML")


def _fmt_user_ref(name: str | None, username: str | None) -> str:
    """@username если есть, иначе имя."""
    if username:
        return f"@{username}"
    return name or "—"


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список всех пользователей (только для админов)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return

    from datetime import datetime as _dt
    users = get_all_users_with_details()

    lines = [f"👥 Все пользователи ({len(users)}):"]
    for i, (_, tid, name, uname, created_at) in enumerate(users, 1):
        try:
            date_fmt = _dt.fromisoformat(created_at).strftime("%d.%m")
        except Exception:
            date_fmt = "—"
        name_str = name or "—"
        uname_str = f" (@{uname})" if uname else ""
        lines.append(f"{i}. {name_str}{uname_str} — {date_fmt}")

    text = "\n".join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список пользователей по подключённым сервисам (только для админов)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return

    all_users = get_all_users_with_details()
    all_tids_ordered = [(tid, name, uname) for _, tid, name, uname, _ in all_users]

    service_defs = [
        ("strava", "🟠 Strava"),
        ("whoop",  "⚪ Whoop"),
        ("garmin", "🔵 Garmin"),
        ("coros",  "🔴 COROS"),
        ("polar",  "❄️ Polar"),
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

    lines = ["📊 Пользователи по сервисам:\n"]
    for svc, label in service_defs:
        rows = service_users[svc]
        refs = ", ".join(_fmt_user_ref(n, u) for _, n, u in rows)
        lines.append(f"{label} ({len(rows)}): {refs or '—'}")

    refs = ", ".join(_fmt_user_ref(n, u) for _, n, u in only_profile)
    lines.append(f"\n👤 Только профиль ({len(only_profile)}): {refs or '—'}")

    refs = ", ".join(_fmt_user_ref(n, u) for _, n, u in nothing)
    lines.append(f"❌ Ничего не подключено ({len(nothing)}): {refs or '—'}")

    inactive = get_inactive_users()
    if inactive:
        refs = ", ".join(_fmt_user_ref(n, u) for _, n, u in inactive)
        lines.append(f"\n💤 Неактивных (заблокировали бота) ({len(inactive)}): {refs}")

    text = "\n".join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает распарсенные данные последней тренировки (только для админов)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    if not last_workout:
        await update.message.reply_text("Тренировка ещё не загружалась. Сначала /workout.")
        return

    w = last_workout
    lines = [
        f"📅 {w.get('weekday', '').capitalize()} {w.get('workout_date', '')}",
        f"Тип: {w.get('workout_type', '—')}",
        f"📍 {w.get('location', '—')}",
        f"⏰ {w.get('schedule', '—')}",
        f"📏 Объём: {w.get('total_volume_km', '—')}",
        f"is_past: {w.get('is_past', False)}",
        "",
        "РАБОТА:",
        w.get('work_text', '—') or '—',
        "",
        "ГРУППЫ:",
        w.get('groups_raw', '—') or '—',
    ]
    extra = w.get('extra_groups', [])
    if extra:
        nums = ', '.join(g['number'] for g in extra)
        lines += ["", f"Доп. группы из комментариев: {nums}"]
        for raw in w.get('extra_groups_raw', []):
            lines.append(f"---\n{raw[:300]}")

    text = '\n'.join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_debug_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает распарсенные данные последнего Long Run (только для админов)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    if not last_long_run:
        await update.message.reply_text("Long Run ещё не загружался. Сначала /long.")
        return

    w = last_long_run
    lines = [
        f"📅 {w.get('weekday', '').capitalize()} {w.get('workout_date', '')}",
        f"Тип: {w.get('workout_type', '—')}",
        f"📍 {w.get('location', '—')}",
        f"⏰ {w.get('schedule', '—')}",
        f"📏 Объём: {w.get('total_volume_km', '—')}",
        f"even_pace_available: {w.get('even_pace_available', False)}",
        f"is_past: {w.get('is_past', False)}",
        "",
        "ГРУППЫ (распарсенные):",
    ]
    for g in (w.get("groups") or []):
        label = g.get("label") or f"Группа {g.get('number', '?')}"
        pace_start = g.get("pace_start", "—")
        pace_end = g.get("pace_end", "—")
        prog = "прогрессия" if g.get("progression") else "ровный"
        lines.append(f"  {label}: {pace_start} → {pace_end} ({prog})")

    lines += ["", "RAW ТЕКСТ (первые 2000 симв.):", (w.get("groups_raw") or "")[:2000]]

    text = '\n'.join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_ratings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Последние 20 оценок рекомендаций (только для админов)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    rows = get_recent_ratings(20)
    if not rows:
        await update.message.reply_text("Оценок пока нет.")
        return
    lines = ["⭐ Последние оценок (до 20):\n"]
    for r in rows:
        rating, ai_mode_, comment, created_at, workout_date, name, username = (
            r[1], r[2], r[3], r[4], r[5], r[6], r[7]
        )
        date_fmt = (created_at or "")[:10]
        uname = f" (@{username})" if username else ""
        stars = rating * "⭐" if rating >= 8 else (rating * "🟡" if rating >= 5 else rating * "🔴")
        comment_str = f"\n   💬 {comment}" if comment else ""
        lines.append(f"{rating}/10 — {name}{uname} [{workout_date}] {date_fmt} [{ai_mode_}]{comment_str}")
    text = "\n".join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_feedbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Последние 20 сообщений обратной связи (только для админов)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    rows = get_recent_feedbacks(20)
    if not rows:
        await update.message.reply_text("Обратной связи пока нет.")
        return
    lines = ["💬 Последние сообщения (до 20):\n"]
    for r in rows:
        fb_type, fb_text, created_at, name, username = r[1], r[2], r[3], r[4], r[5]
        date_fmt = (created_at or "")[:10]
        uname = f" (@{username})" if username else ""
        type_emoji = "🐛" if fb_type == "bug" else "💡"
        lines.append(f"{type_emoji} {name}{uname} [{date_fmt}]\n{fb_text[:300]}")
    text = "\n\n".join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


# ── ДВУХШАГОВАЯ ОБРАБОТКА (анализ тренировок) ────────────────

def _format_analysis_result(result: dict, mode: str) -> str:
    """Красиво форматирует результат analyze_workout для админа."""
    stats = result.get("_stats", {})
    time_sec = stats.get("time_sec", "?")
    lines = [f"🔬 Анализ тренировки (режим {mode}, {time_sec}с)\n"]

    if result.get("is_valid"):
        lines.append("✅ Валидный анонс")
    else:
        lines.append(f"❌ Не анонс: {result.get('reject_reason') or '—'}")

    wtype = result.get("workout_type", "—")
    lines.append(f"Тип: {wtype} | Дата: {result.get('workout_date', '—')}")
    lines.append(f"\n📋 Суть: {result.get('summary', '—')}")

    structure = result.get("structure") or []
    groups = result.get("groups") or []

    if wtype == "interval" and structure:
        # Новая схема: структура один раз + группы как темпы по блокам
        lines.append("\n🏗 Структура:")
        for b in structure:
            blk = b.get("block", "?")
            if b.get("type") == "easy":
                desc = b.get("description") or "лёгкий бег"
                lines.append(f"  Блок {blk}: {b.get('distance_m', '?')}м — {desc}")
            else:
                purpose = f" — {b['purpose']}" if b.get("purpose") else ""
                lines.append(
                    f"  Блок {blk}: {b.get('reps', '?')}×{b.get('work_distance_m', '?')}м"
                    f" / {b.get('recovery_distance_m', '?')}м восст{purpose}"
                )
        if result.get("overall_purpose"):
            lines.append(f"🎯 Цель тренировки: {result['overall_purpose']}")
        if result.get("block_contrast"):
            lines.append(f"🔀 Контраст блоков: {result['block_contrast']}")
        if result.get("target_athlete"):
            lines.append(f"🏃 Для кого: {result['target_athlete']}")
        if result.get("intensity_level"):
            lines.append(f"🔥 Тяжесть: {result['intensity_level']}")
        if result.get("what_to_watch"):
            lines.append(f"👀 На что обратить внимание: {result['what_to_watch']}")
        if result.get("total_volume_km") is not None:
            lines.append(f"📏 Объём: {result['total_volume_km']} км")
        if result.get("is_borderline"):
            note = result.get("borderline_note")
            lines.append(f"⚖️ Пограничная: да{f' — {note}' if note else ''}")

        lines.append(f"\nГруппы ({len(groups)}):")
        for g in groups:
            num = g.get("number", "?")
            tags = []
            if g.get("from_comment"):
                tags.append("💬из комм.")
            if g.get("reps_override"):
                tags.append(f"повторов: {g['reps_override']}")
            if g.get("track_note"):
                tags.append(str(g["track_note"]))
            tag_str = f" ({'; '.join(tags)})" if tags else ""

            if g.get("health_group"):
                lines.append(f"  {num}{tag_str}: бег/ходьба чередование (для начинающих)")
                continue

            block_strs = []
            for bl in (g.get("blocks") or []):
                ar = "🟢" if bl.get("active_recovery") else "⚪"
                rp = bl.get("recovery_pace") or "—"
                ps = bl.get("work_pace_start") or bl.get("work_pace") or "—"
                pe = bl.get("work_pace_end")
                pace_str = f"{ps}→{pe}" if pe and pe != ps else ps
                block_strs.append(
                    f"бл{bl.get('block', '?')} {pace_str}/км (восст {rp} {ar})"
                )
            body = "; ".join(block_strs) if block_strs else "—"
            lines.append(f"  {num}{tag_str}: {body}")
    else:
        # Long или старый формат: группы с текстовым work
        lines.append(f"\nГруппы ({len(groups)}):")
        for g in groups:
            recovery = g.get("recovery")
            line = f"  {g.get('number', '?')}. {g.get('work', '—')}"
            if recovery and str(recovery).lower() != "none":
                ar = " 🟢актив.восст." if g.get("active_recovery") else ""
                rec_pace = g.get("recovery_pace") or "—"
                line += f"\n     ↻ восст: {recovery} ({rec_pace}){ar}"
            lines.append(line)

    extra = result.get("extra_groups") or []
    if extra:
        lines.append(f"\nДоп. группы ({len(extra)}):")
        for e in extra:
            lines.append(f"  {e.get('number', '?')}: {e.get('description', '—')} [{e.get('source', '—')}]")
    else:
        lines.append("\nДоп. группы: нет")

    if wtype == "long":
        prog = "да" if result.get("has_progression") else "нет"
        even = "да" if result.get("even_pace_available") else "нет"
        lines.append(f"\nПрогрессия: {prog} | Ровный темп: {even}")

    lines.append(f"\n📝 Заметки тренера: {result.get('coach_notes') or '—'}")
    lines.append(f"🗑 Проигнорировано: {result.get('ignored') or '—'}")
    return "\n".join(lines)


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор типа тренировки для анализа через DeepSeek (только для админов)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    await update.message.reply_text(
        "Какую тренировку проанализировать?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚡ Интервальная (вт/пт)", callback_data="analyze_interval"),
            InlineKeyboardButton("🕐 Long Run (вс)",        callback_data="analyze_long"),
        ]])
    )


async def _run_analyze_and_show(workout: dict, query, context: ContextTypes.DEFAULT_TYPE):
    """Анализирует найденный пост тренировки и показывает результат админу."""
    raw_text = workout.get("raw_text", "")
    comments_text = workout.get("comments_text", "")
    post_id = workout.get("post_id")

    if not raw_text:
        await query.edit_message_text("❌ Не удалось получить текст поста для анализа.")
        return

    mode = get_preprocess_mode()
    await query.edit_message_text(
        f"⏳ Анализирую через DeepSeek (режим {mode})...\nМожет занять 1-2 минуты."
    )

    import functools
    result = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(analyze_workout, raw_text, comments_text, mode)
    )

    if not result:
        await query.edit_message_text("❌ Анализ не удался (пустой ответ модели). Попробуй ещё раз.")
        return

    # Сохраняем результат в БД
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
    label = "🧠 deep (deepseek-v4-pro)" if current == "deep" else "⚡ smart (deepseek-v4-flash)"
    return (
        "🔬 Режим анализа тренировок (preprocess)\n\n"
        f"Текущий: {label}\n\n"
        "🧠 deep — медленнее, максимальное качество\n"
        "⚡ smart — быстрее, чуть проще\n\n"
        "Выбери режим:"
    )


def _build_preprocess_keyboard(current: str) -> InlineKeyboardMarkup:
    def btn(key, label):
        mark = "✓ " if key == current else ""
        return InlineKeyboardButton(f"{mark}{label}", callback_data=f"preprocess_set_{key}")
    return InlineKeyboardMarkup([
        [btn("deep", "🧠 deep"), btn("smart", "⚡ smart")],
    ])


async def cmd_preprocess_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключатель режима анализа тренировок deep/smart (только для админов)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    current = get_preprocess_mode()
    await update.message.reply_text(
        _build_preprocess_text(current),
        reply_markup=_build_preprocess_keyboard(current),
    )


# ── ТЕСТ ШАГА 2 (рекомендация на готовом анализе) ────────────

async def _collect_admin_user_data(db_user_id: int) -> tuple[dict, list[str]]:
    """Собирает user_data текущего админа для recommend_*. Возвращает (user_data, missing)."""
    missing = []
    profile = get_user_profile(db_user_id)
    spec = (profile or {}).get("specialization")

    zinfo = zones.get_pace_zones(db_user_id)
    if not zinfo or not zinfo.get("zones"):
        missing.append("персональные зоны (нет VO2max/ЛП в профиле)")

    recovery = None
    try:
        recovery = await _get_recovery_data(db_user_id, force_fresh=True)
    except Exception as e:
        logger.warning(f"test: recovery error for {db_user_id}: {e}")
    if not recovery:
        missing.append("восстановление (Whoop/Garmin/COROS/Polar) — взято нейтральное 70")

    user_data = {"db_user_id": db_user_id, "specialization": spec, "recovery": recovery}
    return user_data, missing


async def _run_test_step2(update, context, *, long: bool):
    """Общая логика /test_workout и /test_long."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return

    user = update.effective_user
    db_user_id = get_or_create_user(user.id, user.full_name, user.username)
    label = "Long Run" if long else "интервальную"
    msg = await update.message.reply_text(f"🧪 Ищу {label} тренировку в канале...")

    workout = await (find_next_long_run() if long else find_next_workout(only_interval=True))
    if not workout:
        await msg.edit_text("😔 Не нашёл подходящую тренировку в канале.")
        return

    mode = get_preprocess_mode()
    await msg.edit_text(f"🧪 Анализирую через DeepSeek (режим {mode})...\nМожет занять 1-2 минуты.")

    import functools
    analysis = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(analyze_workout, workout["raw_text"], workout["comments_text"], mode)
    )
    if not analysis:
        await msg.edit_text("❌ Анализ не удался (пустой ответ модели).")
        return

    user_data, missing = await _collect_admin_user_data(db_user_id)

    if long:
        rec = claude_advisor.recommend_long(analysis, user_data)
    else:
        rec = claude_advisor.recommend_group(analysis, user_data)

    # ── Заголовок теста с пометкой чего не хватило ────────────
    header = [
        f"🧪 <b>Тест Шага 2 — {'Long Run' if long else 'Интервальная'}</b>",
        f"Анализ: {analysis.get('workout_date', '—')} · "
        f"valid={analysis.get('is_valid')} · режим {mode}",
    ]
    if not long:
        header.append(f"is_borderline: {analysis.get('is_borderline')}")
    if missing:
        header.append("⚠️ Не хватило данных: " + "; ".join(missing))
    else:
        src = (rec or {}).get("zones_source")
        header.append(f"✅ Данные полные (зоны: {src})")

    await msg.edit_text("\n".join(header), parse_mode="HTML")

    # ── Сам вывод рекомендации (как увидит пользователь) ──────
    if not rec or not rec.get("ok"):
        note = (rec or {}).get("note", "рекомендация недоступна")
        await context.bot.send_message(user.id, f"❌ {note}")
        return

    rec_text = rec.get("text", "(пустой вывод)")
    for i in range(0, len(rec_text), 4096):
        await context.bot.send_message(user.id, rec_text[i:i + 4096])


async def cmd_test_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тест Шага 2 для интервальной: анализ + recommend_group на данных админа (админ)."""
    await _run_test_step2(update, context, long=False)


async def cmd_test_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тест Шага 2 для длительной: анализ + recommend_long на данных админа (админ)."""
    await _run_test_step2(update, context, long=True)


async def _reanalyze_one(workout: dict, mode: str) -> str:
    """Принудительный переанализ одного анонса (игнор idempotency) → запись в workout_analysis.
    Возвращает строку статуса для админа.
    """
    import json as _json, functools
    label = "🕐 Long Run" if workout.get("workout_type") == "long" else "⚡ Интервальная"
    date_fmt = workout.get("workout_date", "—")
    raw_text = workout.get("raw_text") or ""
    comments_text = workout.get("comments_text") or ""
    edit_date = workout.get("edit_date")
    extra = workout.get("extra_groups") or []

    result = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(analyze_workout, raw_text, comments_text, mode)
    )
    if not result:
        return f"{label} — {date_fmt}: ❌ анализ не удался (пустой ответ модели)"

    # Failsafe B: анонс без групп физически бесполезен для рекомендации
    if result.get("is_valid") and not (result.get("groups") or []):
        result["is_valid"] = False
        result["reject_reason"] = "нет групп с темпами — не анонс"

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
    logger.info(f"reanalyze: post_id={workout.get('post_id')} обновлён вручную "
                f"(type={result.get('workout_type')}, valid={result.get('is_valid')})")
    return (
        f"{label} — {result.get('workout_date', date_fmt)} | режим {mode} | "
        f"valid={result.get('is_valid')}\n"
        f"   групп: {n_groups}, доп. групп: {n_extra} — обновлено"
    )


async def cmd_reanalyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной форс переанализа свежих анонсов (interval + long) → кэш workout_analysis (админ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return

    mode = get_preprocess_mode()
    msg = await update.message.reply_text(
        f"🔁 Принудительный переанализ свежих анонсов (режим {mode})...\nМожет занять 1-2 минуты на каждый."
    )

    lines = [f"🔁 <b>Переанализ выполнен (режим {mode})</b>\n"]

    workout = await find_next_workout(only_interval=True)
    if workout and workout.get("post_id"):
        lines.append(await _reanalyze_one(workout, mode))
    else:
        lines.append("⚡ Интервальная — анонс не найден")

    workout_lr = await find_next_long_run()
    if workout_lr and workout_lr.get("post_id"):
        lines.append(await _reanalyze_one(workout_lr, mode))
    else:
        lines.append("🕐 Long Run — анонс не найден")

    await msg.edit_text("\n".join(lines), parse_mode="HTML")


async def cmd_show_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает последний Шаг 1 из базы без нового запроса к модели (админ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    await update.message.reply_text(
        "Показать последний анализ (Шаг 1) из базы:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚡ Интервальная", callback_data="show_analyze_interval"),
            InlineKeyboardButton("🕐 Long Run",     callback_data="show_analyze_long"),
        ]])
    )


# ── КНОПКИ ───────────────────────────────────────────────────

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
        # Приходит из кнопок на рекомендации — отправляем НОВЫМ сообщением,
        # чтобы не перезаписывать текст рекомендации.
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        text, keyboard = _build_main_menu_content(user, db_user_id)
        await context.bot.send_message(user.id, text, reply_markup=keyboard)

    elif query.data == "show_settings":
        # Только кнопки меняем, текст оставляем
        await query.edit_message_reply_markup(reply_markup=_build_screen2_keyboard())

    elif query.data == "settings_menu":
        # Возврат в Экран 2 из вложенных разделов (профиль / уведомления / сервисы)
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        text, _ = _build_main_menu_content(user, db_user_id)
        await query.edit_message_text(text, reply_markup=_build_screen2_keyboard())

    elif query.data == "show_services":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        # Только кнопки меняем, текст оставляем
        await query.edit_message_reply_markup(
            reply_markup=_build_screen3_keyboard(db_user_id)
        )

    elif query.data == "connect_strava":
        auth_url = get_auth_url(user.id)
        keyboard = [
            [InlineKeyboardButton("🔗 Войти в Strava", url=auth_url)],
            [InlineKeyboardButton("← Назад", callback_data="show_services")],
        ]
        await query.edit_message_text(
            "⚠️ Strava временно ограничена — подключение новых пользователей на проверке у Strava.\n"
            "Используй Garmin или COROS (/connect_garmin, /connect_coros) для полноценной работы.\n\n"
            "Нажми кнопку и авторизуйся в Strava.\n\n"
            "После авторизации ты автоматически получишь сообщение в Telegram — ничего копировать не нужно.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "connect_whoop_btn":
        from whoop import get_auth_url as whoop_auth_url
        auth_url = whoop_auth_url(user.id)
        keyboard = [
            [InlineKeyboardButton("🔗 Войти в Whoop", url=auth_url)],
            [InlineKeyboardButton("← Назад", callback_data="show_services")],
        ]
        context.user_data["awaiting_whoop_code"] = True
        await query.edit_message_text(
            "Нажми кнопку и авторизуйся в Whoop.\n\n"
            "После авторизации браузер откроет страницу с JSON — "
            "скопируй весь URL из адресной строки и отправь мне сюда.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "connect_garmin_btn":
        context.user_data["awaiting_garmin"] = "email"
        await query.edit_message_text(
            "Подключение Garmin Connect\n\n"
            "Email и пароль хранятся в зашифрованном виде (AES-256).\n\n"
            "Введи email от Garmin Connect:"
        )

    elif query.data == "connect_coros_btn":
        context.user_data["awaiting_coros"] = "email"
        await query.edit_message_text(
            "Подключение COROS\n\n"
            "Email и пароль хранятся в зашифрованном виде (AES-256).\n\n"
            "Введи email от аккаунта COROS:"
        )

    elif query.data == "connect_polar_btn":
        from polar import get_auth_url as polar_auth_url
        auth_url = polar_auth_url(user.id)
        if not auth_url:
            await query.edit_message_text(
                "❌ Polar не настроен. Обратитесь к администратору.",
                reply_markup=_build_screen3_keyboard(get_or_create_user(user.id, user.full_name, user.username))
            )
            return
        keyboard = [
            [InlineKeyboardButton("🔗 Войти в Polar", url=auth_url)],
            [InlineKeyboardButton("← Назад", callback_data="show_services")],
        ]
        await query.edit_message_text(
            "Подключение Polar\n\n"
            "Нажми кнопку и авторизуйся в Polar Flow.\n\n"
            "После авторизации ты автоматически получишь сообщение в Telegram — ничего копировать не нужно.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "svc_noop":
        pass  # нажатие на лейбл подключённого сервиса — ничего не делаем

    elif query.data == "svc_cancel":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        await query.edit_message_text(
            "🔗 Подключённые сервисы",
            reply_markup=_build_screen3_keyboard(db_user_id)
        )

    elif query.data.startswith("disc_ask_"):
        svc = query.data[len("disc_ask_"):]
        name = _svc_name(svc)
        await query.edit_message_text(
            f"Отключить {name}?\n\nДанные будут удалены из бота.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, отключить", callback_data=f"disc_yes_{svc}"),
                InlineKeyboardButton("❌ Отмена",        callback_data="svc_cancel"),
            ]])
        )

    elif query.data.startswith("disc_yes_"):
        svc = query.data[len("disc_yes_"):]
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        delete_token(db_user_id, svc)
        done_msg = _svc_done_msg(svc)
        logger.info(f"Сервис {svc} отключён для user {db_user_id}")
        await query.edit_message_text(
            f"✅ {done_msg}.",
            reply_markup=_build_screen3_keyboard(db_user_id)
        )

    elif query.data == "get_morning":
        msg = await context.bot.send_message(user.id, "☀️ Проверяю твоё восстановление...")
        await _send_morning_check(user.id, context, msg)

    elif query.data == "get_workout":
        msg = await context.bot.send_message(user.id, "🔍 Ищу анонс, анализирую и подбираю группу...")
        await _send_recommendation(user.id, user.full_name, context, long=False, msg=msg)

    elif query.data == "get_long_run":
        msg = await context.bot.send_message(user.id, "🔍 Подбираю Long Run...")
        await _send_recommendation(user.id, user.full_name, context, long=True, msg=msg)

    elif query.data == "refresh_cache":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        await query.edit_message_text("⏳ Обновляю данные из всех подключённых сервисов...")

        result_lines = ["✅ Данные обновлены!\n"]

        # Strava
        try:
            access_token = await ensure_valid_token(db_user_id)
            if access_token:
                athlete_data = await refresh_athlete_cache(db_user_id, access_token)
                if athlete_data:
                    load = athlete_data["training_load"]
                    result_lines.append(
                        f"🟠 Strava: CTL {load.get('ctl', '—')}, "
                        f"ATL {load.get('atl', '—')}, TSB {load.get('tsb', '—')}"
                    )
        except Exception as e:
            logger.error(f"Strava refresh error (button) for {user.id}: {e}")
            result_lines.append("🟠 Strava: ❌ ошибка")

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
                    garmin_parts.append(f"ЛП {lt['pace']}")
                if not isinstance(readiness, Exception) and readiness and readiness.get("score") is not None:
                    garmin_parts.append(f"TR {readiness['score']}")
                result_lines.append(f"🔵 Garmin: {', '.join(garmin_parts) if garmin_parts else 'обновлено'}")
            except Exception as e:
                logger.error(f"Garmin refresh error (button) for {user.id}: {e}")
                result_lines.append("🔵 Garmin: ❌ ошибка")

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
                result_lines.append(f"🔴 COROS: {', '.join(coros_parts)}" if coros_parts else "🔴 COROS: обновлено")
            except Exception as e:
                logger.error(f"COROS refresh error (button) for {user.id}: {e}")
                result_lines.append("🔴 COROS: ❌ ошибка")

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
                result_lines.append(f"❄️ Polar: {', '.join(polar_parts)}" if polar_parts else "❄️ Polar: обновлено")
            except Exception as e:
                logger.error(f"Polar refresh error (button) for {user.id}: {e}")
                result_lines.append("❄️ Polar: ❌ ошибка")

        if len(result_lines) == 1:
            result_lines.append("Нет подключённых сервисов.\nПодключи трекер в Настройках → Сервисы.")

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
            "Введи значение VO2max (мл/кг/мин).\n\nНапример: 53"
        )

    elif query.data == "profile_set_lactate":
        context.user_data["awaiting_profile"] = "set_lactate_pace"
        await query.edit_message_text(
            "Введи темп лактатного порога (мин:сек на км).\n\nНапример: 4:17"
        )

    elif query.data == "profile_set_gender":
        await query.edit_message_text(
            "Выбери пол:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👨 Мужской", callback_data="profile_gender_male"),
                 InlineKeyboardButton("👩 Женский", callback_data="profile_gender_female")],
            ])
        )

    elif query.data in ("profile_gender_male", "profile_gender_female"):
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        gender = "male" if query.data == "profile_gender_male" else "female"
        gender_label = "Мужской" if gender == "male" else "Женский"
        save_user_profile(db_user_id, gender=gender)
        profile = get_user_profile(db_user_id)
        await query.edit_message_text(
            f"✅ Пол сохранён: {gender_label}\n\n{_build_profile_text(profile)}",
            reply_markup=_build_profile_keyboard(profile)
        )

    elif query.data == "profile_set_specialization":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        current_spec = (get_user_profile(db_user_id) or {}).get("specialization")
        await query.edit_message_text(
            "Выбери специализацию (на что нацелены тренировки):",
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
            f"✅ Специализация сохранена: {SPECIALIZATIONS[spec]}\n\n{_build_profile_text(profile)}",
            reply_markup=_build_profile_keyboard(profile)
        )

    elif query.data in ("profile_toggle_vo2max_lock", "profile_toggle_lactate_lock"):
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        profile = get_user_profile(db_user_id)
        if query.data == "profile_toggle_vo2max_lock":
            new_val = 0 if (profile or {}).get("vo2max_locked") else 1
            save_user_profile(db_user_id, vo2max_locked=new_val)
            note = "🔒 VO2max защищён — сервисы не перепишут." if new_val else "🔓 VO2max будет обновляться из сервисов."
        else:
            new_val = 0 if (profile or {}).get("lactate_locked") else 1
            save_user_profile(db_user_id, lactate_locked=new_val)
            note = "🔒 Лактатный порог защищён — сервисы не перепишут." if new_val else "🔓 Лактатный порог будет обновляться из сервисов."
        profile = get_user_profile(db_user_id)
        await query.edit_message_text(
            f"{note}\n\n{_build_profile_text(profile)}",
            reply_markup=_build_profile_keyboard(profile)
        )

    elif query.data == "ai_mode":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        prefs = get_preferences(db_user_id)
        current_mode = prefs.get("ai_mode", "smart") if prefs else "smart"
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
        await query.edit_message_text(
            _build_mode_text(current_mode),
            reply_markup=_merge_keyboards(_build_mode_keyboard(current_mode), back_btn)
        )

    elif query.data in ("mode_set_deep", "mode_set_smart", "mode_set_fast", "mode_set_calc"):
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        new_mode = query.data.replace("mode_set_", "")
        set_preference(db_user_id, "ai_mode", new_mode)
        emoji, label, timing, _ = _MODE_INFO[new_mode]
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
        await query.edit_message_text(
            f"✅ Режим сохранён: {emoji} {label} ({timing})\n\n{_build_mode_text(new_mode)}",
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
                    "✅ Garmin включён как резервный источник.\n\n"
                    "Whoop подключён и будет в приоритете — "
                    "Garmin (Body Battery, HRV) используется только если данных Whoop нет."
                )
            else:
                answer = "✅ Буду использовать данные Body Battery и HRV из Garmin утром."
        else:
            answer = (
                "Понял, данные восстановления из Garmin использоваться не будут.\n"
                "Изменить можно через /status."
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
            await context.bot.send_message(user.id, "⏱ Данные устарели. Запроси рекомендацию заново (/workout или /long)")
            return
        try:
            from fit_generator import (
                build_garmin_interval_workout, build_garmin_long_run_workout,
                workout_filename,
            )
            import json, io
            # Используем JSON от DeepSeek если есть, иначе парсер
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
                caption=f"🏃 <b>{fname}</b>\n\nИмпортируй в Garmin Connect: Тренировки → ➕ → Из файла.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"JSON generation error for {user.id}: {e}")
            await context.bot.send_message(user.id, f"❌ Ошибка генерации JSON: {type(e).__name__}: {e}")

    elif query.data == "fit_up":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        if not get_token(db_user_id, "garmin"):
            await context.bot.send_message(user.id,
                "❌ Garmin не подключён.\n\nИспользуй /connect_garmin чтобы подключить аккаунт.")
            return
        data = _fit_data.get(user.id)
        if not data:
            await context.bot.send_message(user.id, "⏱ Данные устарели. Запроси рекомендацию заново (/workout или /long)")
            return
        try:
            from fit_generator import build_garmin_interval_workout, build_garmin_long_run_workout
            from garmin import upload_workout as garmin_upload_workout
            # Используем JSON от DeepSeek если есть, иначе парсер
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
                    f"✅ <b>Тренировка загружена в Garmin Connect!</b>\n\n"
                    f"📋 {name}\n\n"
                    f"Открой приложение Garmin Connect → Тренировки и планы → Тренировки.",
                    parse_mode="HTML")
            else:
                await context.bot.send_message(user.id,
                    "❌ Не удалось загрузить в Garmin Connect.\n\n"
                    "Попробуй скачать JSON кнопкой 📥 и импортировать вручную.")
        except Exception as e:
            logger.error(f"Garmin upload error for {user.id}: {e}")
            await context.bot.send_message(user.id,
                f"❌ Ошибка загрузки в Garmin: {type(e).__name__}")

    elif query.data == "help":
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
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

    elif query.data in ("show_analyze_interval", "show_analyze_long"):
        if user.id not in ADMIN_TELEGRAM_IDS:
            return
        import json as _json
        wtype = "long" if query.data == "show_analyze_long" else "interval"
        row, status = get_latest_workout_analysis(wtype)
        if status == "empty" or row is None:
            await query.edit_message_text(f"😔 Нет анализа {wtype} в базе.")
            return
        try:
            result = _json.loads(row.get("analyzed_json") or "{}")
        except Exception:
            result = {}
        mode = row.get("analysis_mode", "?")
        text = _format_analysis_result(result, mode)
        first = True
        for i in range(0, len(text), 4096):
            chunk = text[i:i + 4096]
            if first:
                await query.edit_message_text(chunk)
                first = False
            else:
                await context.bot.send_message(user.id, chunk)

    elif query.data in ("analyze_interval", "analyze_long"):
        if user.id not in ADMIN_TELEGRAM_IDS:
            return
        is_long = query.data == "analyze_long"
        await query.edit_message_text(
            f"🔬 Ищу {'Long Run' if is_long else 'интервальную'} тренировку в канале..."
        )
        workout = await (find_next_long_run() if is_long else find_next_workout(only_interval=True))
        if not workout:
            await query.edit_message_text("😔 Не нашёл подходящую тренировку в канале.")
            return
        await _run_analyze_and_show(workout, query, context)

    # ── ОБРАТНАЯ СВЯЗЬ ────────────────────────────────────────

    elif query.data == "feedback_show":
        await query.edit_message_text("Выбери тип:", reply_markup=_build_feedback_keyboard())

    elif query.data in ("feedback_bug", "feedback_feature"):
        fb_type = "bug" if query.data == "feedback_bug" else "feature"
        type_label = "проблему" if fb_type == "bug" else "идею"
        context.user_data["awaiting_feedback"] = fb_type
        await query.edit_message_text(f"Опиши {type_label}:")

    # ── ОЦЕНКА РЕКОМЕНДАЦИИ ───────────────────────────────────

    elif query.data == "rate_show":
        data = _rating_data.get(user.id)
        if not data:
            await context.bot.send_message(
                user.id, "⏱ Данные устарели. Запроси рекомендацию заново (/workout или /long)."
            )
            return
        context.user_data["rating_pending"] = dict(data)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(str(i), callback_data=f"rate_{i}") for i in range(1, 6)],
            [InlineKeyboardButton(str(i), callback_data=f"rate_{i}") for i in range(6, 11)],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu_new")],
        ])
        await query.edit_message_text(
            "Оцени рекомендацию:\n1 — плохо, 10 — отлично",
            reply_markup=keyboard
        )

    elif query.data.startswith("rate_") and query.data[5:].isdigit():
        rating = int(query.data[5:])
        ctx = context.user_data.get("rating_pending", {})
        ctx["rating"] = rating
        context.user_data["rating_pending"] = ctx
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Написать комментарий", callback_data="rate_comment"),
             InlineKeyboardButton("Пропустить", callback_data="rate_skip")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu_new")],
        ])
        await query.edit_message_text(
            f"Оценка {rating}/10 ✅\n\nХочешь добавить комментарий? (необязательно)",
            reply_markup=keyboard
        )

    elif query.data == "rate_comment":
        context.user_data["awaiting_rating_comment"] = True
        await query.edit_message_text("Напиши комментарий:")

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
                    f"⭐ Низкая оценка: {rating}/10\n"
                    f"От: {user.full_name}{uname}\n"
                    f"Тренировка: {ctx.get('workout_date', '—')}\n"
                    f"Режим: {ctx.get('ai_mode', '—')}\n"
                    f"Комментарий: нет"
                )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu_new")
        ]])
        await query.edit_message_text("✅ Спасибо за оценку!", reply_markup=keyboard)


# ── ОБРАБОТКА ТЕКСТА ─────────────────────────────────────────

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    _mark_user_active_if_needed(user.id, user.full_name, user.username)

    # Код Whoop — пользователь вставляет URL с httpbin.org
    if context.user_data.get("awaiting_whoop_code"):
        import re
        from whoop import exchange_code as whoop_exchange
        code_match = re.search(r'[?&]code=([^&]+)', text)
        code = code_match.group(1) if code_match else text.strip()
        msg = await update.message.reply_text("⏳ Подключаю Whoop...")
        try:
            import time as _t
            token_data = await whoop_exchange(code)
            if "access_token" not in token_data:
                await msg.edit_text("❌ Не удалось подключить Whoop. Попробуй /connect_whoop")
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
                "✅ Whoop подключён! Утренние рекомендации теперь точнее.",
                reply_markup=_build_screen3_keyboard(db_user_id2)
            )
        except Exception as e:
            logger.error(f"Whoop auth error: {e}")
            await msg.edit_text("❌ Ошибка. Попробуй /connect_whoop")
        return

    # Ввод данных профиля
    elif context.user_data.get("awaiting_profile") == "set_vo2max":
        import re
        if not re.match(r'^\d+(?:[.,]\d+)?$', text):
            await update.message.reply_text("Введи число, например: 53")
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
            f"✅ VO2max сохранён: {vo2max} мл/кг/мин\n\n{_build_profile_text(profile)}",
            reply_markup=_build_profile_keyboard(profile)
        )

    elif context.user_data.get("awaiting_profile") == "set_lactate_pace":
        import re
        pace_match = re.match(r'^(\d+:\d{2})$', text.strip())
        if not pace_match:
            await update.message.reply_text("Не распознал темп. Формат: 4:17")
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
            f"Темп {pace} сохранён.\n\nТеперь введи пульс на лактатном пороге.\n\nНапример: 174"
        )

    elif context.user_data.get("awaiting_profile") == "set_lactate_hr":
        import re
        if not re.match(r'^\d{2,3}$', text.strip()):
            await update.message.reply_text("Введи пульс числом, например: 174")
            return
        hr = int(text.strip())
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        save_user_profile(db_user_id, lactate_threshold_hr=hr, lactate_source="manual")
        pace = context.user_data.pop("lactate_pace", "")
        context.user_data.pop("awaiting_profile")
        profile = get_user_profile(db_user_id)
        await update.message.reply_text(
            f"✅ Лактатный порог сохранён: {pace} мин/км при ЧСС {hr} уд/мин\n\n{_build_profile_text(profile)}",
            reply_markup=_build_profile_keyboard(profile)
        )

    # Email для Garmin
    elif context.user_data.get("awaiting_garmin") == "email":
        context.user_data["garmin_email"] = text.strip()
        context.user_data["awaiting_garmin"] = "password"
        await update.message.reply_text(
            "Введи пароль от Garmin Connect:\n\n"
            "Сообщение с паролем будет удалено сразу после отправки."
        )

    # Пароль для Garmin
    elif context.user_data.get("awaiting_garmin") == "password":
        from garmin import connect as garmin_connect, get_vo2max, get_training_readiness
        email = context.user_data.pop("garmin_email", "")
        password = text.strip()
        context.user_data.pop("awaiting_garmin", None)

        # Удаляем сообщение с паролем
        try:
            await update.message.delete()
        except Exception:
            pass

        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        msg = await update.effective_chat.send_message("⏳ Подключаюсь к Garmin Connect...")
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

            lines = ["✅ Garmin подключён!\n"]

            if garmin_vo2max_found or garmin_lt_found or (not isinstance(body_battery, Exception) and body_battery is not None):
                lines.append("Загружено из Garmin:")
                if garmin_vo2max_found:
                    lines.append(f"📊 VO2max: {vo2max:.1f} мл/кг/мин")
                else:
                    lines.append("📊 VO2max: не найден — укажи вручную в /profile")
                if garmin_lt_found:
                    lines.append(f"⚡ Лактатный порог: {lt['pace']} мин/км при ЧСС {lt['hr']}")
                if not isinstance(body_battery, Exception) and body_battery is not None:
                    lines.append(f"🔋 Body Battery: {body_battery}/100")
                if not isinstance(hrv, Exception) and hrv:
                    lines.append(f"💗 HRV: {hrv.get('hrv_last_night', '—')} мс (среднеенедельное: {hrv.get('hrv_weekly_avg', '—')})")
                if not isinstance(readiness, Exception) and readiness and readiness.get("score") is not None:
                    lines.append(f"🎯 Training Readiness: {readiness['score']}/100 ({readiness.get('level', '')})")
            else:
                lines.append("📊 VO2max не найден в данных.")
                lines.append("Укажи его вручную в профиле → /profile")

            if garmin_vo2max_found:
                lines.append("\nХочешь уточнить лактатный порог? → /profile")
                lines.append("Или сразу попробуй /workout")

            lines.append(
                "\nТы носишь Garmin постоянно (включая сон)?\n"
                "Это влияет на то, используем ли Body Battery и HRV утром."
            )
            keyboard = _merge_keyboards(
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Да, постоянно", callback_data="garmin_recovery_yes"),
                    InlineKeyboardButton("🏃 Только на тренировках", callback_data="garmin_recovery_no"),
                ]]),
                InlineKeyboardMarkup([[InlineKeyboardButton("← Сервисы", callback_data="show_services")]])
            )
            await msg.edit_text("\n".join(lines), reply_markup=keyboard)

            n = count_users_with_service("garmin")
            uname = f" (@{user.username})" if user.username else ""
            await _notify_admin(
                context.bot,
                f"🔵 {user.full_name}{uname} подключил Garmin\n"
                f"Всего с Garmin: {n}"
            )
        except Exception as e:
            logger.error(f"Garmin auth error: {e}")
            await msg.edit_text(
                f"❌ Не удалось подключить Garmin Connect.\n"
                f"Проверь правильность email и пароля, затем попробуй /connect_garmin снова.\n\n"
                f"Ошибка: {type(e).__name__}: {e}"
            )

    # Email для COROS
    elif context.user_data.get("awaiting_coros") == "email":
        context.user_data["coros_email"] = text.strip()
        context.user_data["awaiting_coros"] = "password"
        await update.message.reply_text(
            "Введи пароль от COROS:\n\n"
            "Сообщение с паролем будет удалено сразу после отправки."
        )

    # Пароль для COROS
    elif context.user_data.get("awaiting_coros") == "password":
        import coros as _coros
        email = context.user_data.pop("coros_email", "")
        password = text.strip()
        context.user_data.pop("awaiting_coros", None)

        # Удаляем сообщение с паролем
        try:
            await update.message.delete()
        except Exception:
            pass

        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        msg = await update.effective_chat.send_message("⏳ Подключаюсь к COROS...")
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

            lines = ["✅ COROS подключён!\n"]
            lines.append("Загружено из COROS:")

            if coros_vo2max_found:
                lines.append(f"📊 VO2max: {vo2max} мл/кг/мин")
            else:
                lines.append("📊 VO2max: не найден — укажи вручную в /profile")

            if not isinstance(training_load, Exception) and training_load:
                ctl = training_load.get("ctl")
                atl = training_load.get("atl")
                tsb = training_load.get("tsb")
                if ctl is not None:
                    lines.append(f"📈 Training Load: CTL={ctl}, ATL={atl}, TSB={tsb}")

            if not isinstance(hrv, Exception) and hrv:
                hrv_last = hrv.get("hrv_last_night")
                hrv_avg  = hrv.get("hrv_weekly_avg")
                if hrv_last:
                    lines.append(f"💗 HRV: {hrv_last} мс (среднеенедельное: {hrv_avg})")

            if coros_vo2max_found:
                lines.append("\nХочешь уточнить лактатный порог? → /profile")
                lines.append("Или сразу попробуй /workout")

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("← Сервисы", callback_data="show_services")
            ]])
            await msg.edit_text("\n".join(lines), reply_markup=keyboard)

            n = count_users_with_service("coros")
            uname = f" (@{user.username})" if user.username else ""
            await _notify_admin(
                context.bot,
                f"🔴 {user.full_name}{uname} подключил COROS\n"
                f"Всего с COROS: {n}"
            )
        except Exception as e:
            logger.error(f"COROS auth error: {e}")
            await msg.edit_text(
                f"❌ Не удалось подключить COROS.\n"
                f"Проверь правильность email и пароля, затем попробуй /connect_coros снова.\n\n"
                f"Ошибка: {type(e).__name__}: {e}"
            )

    # ── ОБРАТНАЯ СВЯЗЬ (текст) ────────────────────────────────

    elif context.user_data.get("awaiting_feedback"):
        fb_type = context.user_data.pop("awaiting_feedback")
        type_label = "Проблема" if fb_type == "bug" else "Идея"
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        save_feedback(db_user_id, fb_type, text)
        uname = f" (@{user.username})" if user.username else ""
        await _notify_admin(
            context.bot,
            f"💬 Обратная связь [{type_label}]\nОт: {user.full_name}{uname}\n\n{text}"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu_new")
        ]])
        await update.message.reply_text("✅ Спасибо! Сообщение отправлено.", reply_markup=keyboard)
        return

    # ── КОММЕНТАРИЙ К ОЦЕНКЕ ─────────────────────────────────

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
                    f"⭐ Низкая оценка: {rating}/10\n"
                    f"От: {user.full_name}{uname}\n"
                    f"Тренировка: {ctx.get('workout_date', '—')}\n"
                    f"Режим: {ctx.get('ai_mode', '—')}\n"
                    f"Комментарий: {text}"
                )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu_new")
        ]])
        await update.message.reply_text("✅ Спасибо за оценку!", reply_markup=keyboard)
        return


# ── ЛОГИКА РЕКОМЕНДАЦИЙ ──────────────────────────────────────

def _user_has_data(db_user_id: int) -> bool:
    """has_data: есть VO2max в профиле ИЛИ хотя бы один токен трекера."""
    profile = get_user_profile(db_user_id)
    if profile and profile.get("vo2max"):
        return True
    return any(get_token(db_user_id, s) for s in ("strava", "garmin", "coros", "polar"))


async def _send_ai_variant_b(
    telegram_id: int,
    analysis: dict,
    user_data: dict,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Вариант B: чистая ИИ-рекомендация для админа.
    Запускается асинхронно после основного сообщения.
    """
    import functools
    db_user_id = user_data.get("db_user_id")
    import zones as _zones_mod
    zinfo = _zones_mod.get_pace_zones(db_user_id) if db_user_id is not None else None
    zones_map = (zinfo or {}).get("zones") or {}
    recovery = user_data.get("recovery")
    rec_mode = (get_preferences(db_user_id) or {}).get("ai_mode", "smart")

    try:
        advice, stats = await asyncio.get_event_loop().run_in_executor(
            None,
            functools.partial(
                claude_advisor.generate_ai_b_recommendation,
                analysis, user_data, zones_map, recovery, rec_mode
            )
        )
        stats["mode"] = "b_ai"
        # Формируем workout dict из analysis для единого рендерера
        workout_for_render = {
            "workout_type": analysis.get("workout_type", "interval"),
            "workout_date": analysis.get("workout_date", ""),
            "location": analysis.get("location", ""),
            "schedule": "",
            "work_text": "",
        }
        msg_text = claude_advisor.format_evening_message(
            advice, workout_for_render, stats
        )
        await context.bot.send_message(telegram_id, msg_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"_send_ai_variant_b error: {e}")


async def _send_recommendation(
    telegram_id: int, name: str,
    context: ContextTypes.DEFAULT_TYPE,
    long: bool = False,
    msg=None,
    live: dict | None = None,
):
    """И3: рекомендация из кэша workout_analysis + recommend_group/recommend_long.
    find_next_* используется ТОЛЬКО для детекта свежести анонса (post_id/edit_date) и
    как источник упрощённого текста для has_data=False — НЕ для парсинга рекомендации.
    live можно передать заранее (рассылка фетчит один раз на всех).
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
        what = "ближайшего Long Run" if long else "ближайшей тренировки"
        await _out(f"😔 Не нашёл анонс {what} в канале. Попробуй позже.")
        return

    # Плашку past НЕ дублируем для interval — её рисует сам форматтер (is_past).
    banner = ""
    if status == "analyzing":
        banner = ("🔄 Новый анонс появился, сейчас в проработке — обновится через пару минут.\n"
                  "Пока показываю предыдущую тренировку.\n\n")
    elif status == "past" and long:
        banner = ("📅 Будущих тренировок пока нет. Показываю последнюю прошедшую "
                  "(для ознакомления, не на сегодня).\n\n")

    # has_data=False → упрощённое уведомление
    if not _user_has_data(db_user_id):
        if live:
            simple = _build_simple_workout_text(live)
        else:
            simple = (f"📢 Тренировка {row.get('workout_date', '')}\n\n"
                      "Заполни профиль и подключи трекер, чтобы получить рекомендацию группы. "
                      "Новичкам подойдёт группа здоровья (бег/ходьба).")
        await _out(banner + simple)
        return

    # has_data=True → персональная рекомендация из кэша
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
        note = (rec or {}).get("note", "Не удалось собрать рекомендацию. Попробуй позже.")
        await _out(banner + note)
        return

    _rating_data[telegram_id] = {
        "workout_date": analysis.get("workout_date", ""),
        "ai_mode": row.get("analysis_mode", ""),
    }
    rating_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐ Оценить рекомендацию", callback_data="rate_show"),
    ]])
    final_markup = _merge_keyboards(rating_markup, get_main_keyboard(from_recommendation=True))

    # Шапка/погода из live (для current/past совпадает с кэшем)
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

    # Числа/структура — формулами (детерминированно)
    if long:
        advice = claude_advisor.recommendation_to_long_advice(rec, analysis, user_data["recovery"])
    else:
        advice = claude_advisor.recommendation_to_advice(rec, analysis, user_data["recovery"])

    # Режим рекомендации (Шаг 2) из настроек пользователя; анализ (Шаг 1) всегда deep
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
        "athlete_name": name or None,
    }
    import functools
    prose, stats2 = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(claude_advisor.generate_step2_prose, facts, rec_mode, long))
    # ИИ-проза поверх посчитанного (фолбэк на шаблон, если модель не ответила)
    if prose.get("reason"):
        advice["reason"] = prose["reason"]
    if long and prose.get("strategy_reason"):
        advice["strategy_reason"] = prose["strategy_reason"]

    # Футер отражает СВОЙ экран — режим/стоимость рекомендации (Шаг 2), не анализа
    if long:
        body = claude_advisor.format_long_run_message(
            advice, workout_dict, stats=stats2, weather_line=weather_line, has_tracker=has_tracker)
    else:
        body = claude_advisor.format_evening_message(
            advice, workout_dict, stats=stats2, weather_line=weather_line, has_tracker=has_tracker)

    await _out(banner + body, final_markup, parse_mode="HTML")

    # ── Вариант B: чистый ИИ — только для админа ──────────────────────
    if telegram_id in ADMIN_TELEGRAM_IDS and not long:
        asyncio.create_task(_send_ai_variant_b(telegram_id, analysis, user_data, context))


async def _send_workout_recommendation(
    telegram_id: int, name: str,
    context: ContextTypes.DEFAULT_TYPE,
    msg=None
):
    db_user_id = get_or_create_user(telegram_id, name)

    global last_workout

    # 1. Находим тренировку
    workout = await find_next_workout()
    if not workout:
        text = "😔 Не нашёл анонс ближайшей тренировки в канале. Попробуй позже."
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

    # 2. Данные спортсмена: Garmin → COROS → Polar → Strava
    fitness = None

    fitness = await get_garmin_fitness_data(db_user_id)
    if fitness:
        logger.info(f"Источник данных: garmin для user {db_user_id}")

    if not fitness:
        fitness = await get_coros_fitness_data(db_user_id)
        if fitness:
            logger.info(f"Источник данных: coros для user {db_user_id}")

    if not fitness:
        fitness = await get_polar_fitness_data(db_user_id)
        if fitness:
            logger.info(f"Источник данных: polar для user {db_user_id}")

    if not fitness:
        access_token = await ensure_valid_token(db_user_id)
        if access_token:
            try:
                fitness = await get_fitness_data(db_user_id, access_token)
                if fitness:
                    logger.info(f"Источник данных: strava для user {db_user_id}")
            except Exception as e:
                logger.error(f"Strava error for {telegram_id}: {e}")

    if not fitness:
        _profile = get_user_profile(db_user_id)
        if _profile and _profile.get("vo2max") and _profile.get("lactate_threshold_pace"):
            fitness = {
                "source": "profile", "profile_only": True,
                "summary": "Данные только из профиля (без трекера)",
                "total_km": 0, "run_count": 0,
                "avg_pace": "—", "avg_hr": None, "fatigue_level": "unknown",
                "vo2max": _profile["vo2max"],
                "vo2max_source": _profile.get("vo2max_source") or "профиль",
            }
            if _profile.get("lactate_threshold_pace"):
                fitness["lactate_threshold_pace"] = _profile["lactate_threshold_pace"]
            if _profile.get("lactate_threshold_hr"):
                fitness["lactate_threshold_hr"] = _profile["lactate_threshold_hr"]
            if _profile.get("gender"):
                fitness["gender"] = _profile["gender"]
            logger.info(f"Источник данных: profile_only (fast mode) для user {db_user_id}")
        else:
            logger.info(f"Источник данных: нет данных для user {db_user_id}")
            text = format_workout_message(workout)
            text += "\n\nПодключи Garmin (/connect_garmin), COROS (/connect_coros) или Polar (/connect_polar) для рекомендации группы"
            if msg:
                await msg.edit_text(text)
            else:
                await context.bot.send_message(telegram_id, text)
            return

    # 3. Профиль спортсмена (VO2max / лактатный порог из ручного ввода)
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
                fitness["vo2max_source"] = source or "профиль"
        if profile.get("lactate_threshold_pace"):
            fitness["lactate_threshold_pace"] = profile["lactate_threshold_pace"]
        if profile.get("lactate_threshold_hr"):
            fitness["lactate_threshold_hr"] = profile["lactate_threshold_hr"]
        if profile.get("gender"):
            fitness["gender"] = profile["gender"]

    # 4. Данные восстановления (Whoop / Garmin) — всегда свежие для /workout
    recovery = await _get_recovery_data(db_user_id, force_fresh=True)

    # 5. Погода
    weather = await get_weather_for_workout(
        workout.get("location", ""),
        workout.get("workout_date", ""),
        workout.get("schedule", ""),
    )
    weather_line = format_weather_for_message(weather) if weather else ""
    weather_prompt = format_weather_for_prompt(weather) if weather else ""

    # 6. Groq рекомендует
    _profile_only = fitness.get("profile_only", False)
    prefs = get_preferences(db_user_id)
    ai_mode = prefs.get("ai_mode", "smart") if prefs else "smart"
    if _profile_only:
        ai_mode = "fast"  # profile-only → принудительно fast
    wait_msg = {"deep": "🧠 Думаю над рекомендацией... (~2-3 минуты)", "smart": "⚡ Анализирую... (~1-2 минуты)", "fast": "🔥 Считаю быстро... (~30 секунд)"}.get(ai_mode, "⚡ Анализирую... (~1-2 минуты)")
    if msg:
        await msg.edit_text(wait_msg)
    prompt = build_evening_prompt(workout, fitness, recovery, weather_prompt=weather_prompt)
    # Запускаем в executor чтобы не блокировать event loop
    import functools
    result = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(ask_groq, prompt, ai_mode))
    if result and result.get("timeout"):
        timeout_text = "⏱ Модель думает слишком долго. Попробуй ⚡ Умный режим (/mode)"
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
            logger.error(f"Не удалось сохранить рекомендацию: {e}")

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
            InlineKeyboardButton("📥 Скачать JSON", callback_data="fit_dl"),
            InlineKeyboardButton("⌚ Загрузить в Garmin", callback_data="fit_up"),
        ]])
        rating_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("⭐ Оценить рекомендацию", callback_data="rate_show"),
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

    is_long_run_day = datetime.now().weekday() == 6  # воскресенье
    workout = await (find_next_long_run() if is_long_run_day else find_next_workout())
    if not workout:
        text = "😔 Не нашёл тренировку. Отдыхай!"
        if msg:
            await msg.edit_text(text)
        else:
            await context.bot.send_message(telegram_id, text)
        return

    # Данные спортсмена: Garmin → COROS → Polar → Strava
    fitness = None

    fitness = await get_garmin_fitness_data(db_user_id)
    if fitness:
        logger.info(f"Источник данных: garmin для user {db_user_id}")

    if not fitness:
        fitness = await get_coros_fitness_data(db_user_id)
        if fitness:
            logger.info(f"Источник данных: coros для user {db_user_id}")

    if not fitness:
        fitness = await get_polar_fitness_data(db_user_id)
        if fitness:
            logger.info(f"Источник данных: polar для user {db_user_id}")

    if not fitness:
        access_token = await ensure_valid_token(db_user_id)
        if access_token:
            try:
                fitness = await get_fitness_data(db_user_id, access_token)
                if fitness:
                    logger.info(f"Источник данных: strava для user {db_user_id}")
            except Exception as e:
                logger.error(f"Strava morning error: {e}")

    if not fitness:
        _profile = get_user_profile(db_user_id)
        if _profile and _profile.get("vo2max") and _profile.get("lactate_threshold_pace"):
            fitness = {
                "source": "profile", "profile_only": True,
                "summary": "Данные только из профиля (без трекера)",
                "total_km": 0, "run_count": 0,
                "avg_pace": "—", "avg_hr": None, "fatigue_level": "unknown",
                "vo2max": _profile["vo2max"],
                "vo2max_source": _profile.get("vo2max_source") or "профиль",
            }
            if _profile.get("lactate_threshold_pace"):
                fitness["lactate_threshold_pace"] = _profile["lactate_threshold_pace"]
            if _profile.get("lactate_threshold_hr"):
                fitness["lactate_threshold_hr"] = _profile["lactate_threshold_hr"]
            if _profile.get("gender"):
                fitness["gender"] = _profile["gender"]
            logger.info(f"Источник данных: profile_only для user {db_user_id}")
        else:
            logger.info(f"Источник данных: нет данных для user {db_user_id}")
            fitness = {"summary": "Нет данных", "total_km": 0, "run_count": 0,
                       "avg_pace": "—", "avg_hr": None, "fatigue_level": "unknown"}

    # Whoop / Garmin / COROS
    recovery = await _get_recovery_data(db_user_id)

    if not recovery:
        has_garmin = bool(get_token(db_user_id, "garmin"))
        prefs = get_preferences(db_user_id)
        garmin_disabled = has_garmin and not (prefs.get("use_garmin_recovery", True) if prefs else True)
        if garmin_disabled:
            hint = "Garmin подключён, но данные восстановления отключены — включи в /status"
        elif has_garmin:
            hint = "Garmin подключён, но данные за ночь не получены (возможно, часы не синхронизированы)"
        else:
            hint = "Подключи Whoop, Garmin (/connect_garmin) или COROS (/connect_coros) для точных рекомендаций"
        text = (
            "☀️ Доброе утро!\n\n"
            f"Нет данных о восстановлении. {hint}.\n\n"
            "Прислушайся к своим ощущениям:\n"
            "• Если чувствуешь себя хорошо — иди по плану\n"
            "• Если устал — снизь темп на группу ниже"
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
        timeout_text = "⏱ Модель думает слишком долго. Попробуй ⚡ Умный режим (/mode)"
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
        text = "😔 Не нашёл анонс Long Run в канале. Попробуй позже."
        if msg:
            await msg.edit_text(text)
        else:
            await context.bot.send_message(telegram_id, text)
        return

    last_long_run = workout

    fitness = None

    fitness = await get_garmin_fitness_data(db_user_id)
    if fitness:
        logger.info(f"Источник данных: garmin для user {db_user_id}")

    if not fitness:
        fitness = await get_coros_fitness_data(db_user_id)
        if fitness:
            logger.info(f"Источник данных: coros для user {db_user_id}")

    if not fitness:
        fitness = await get_polar_fitness_data(db_user_id)
        if fitness:
            logger.info(f"Источник данных: polar для user {db_user_id}")

    if not fitness:
        access_token = await ensure_valid_token(db_user_id)
        if access_token:
            try:
                fitness = await get_fitness_data(db_user_id, access_token)
                if fitness:
                    logger.info(f"Источник данных: strava для user {db_user_id}")
            except Exception as e:
                logger.error(f"Strava long run error for {telegram_id}: {e}")

    if not fitness:
        _profile = get_user_profile(db_user_id)
        if _profile and _profile.get("vo2max") and _profile.get("lactate_threshold_pace"):
            fitness = {
                "source": "profile", "profile_only": True,
                "summary": "Данные только из профиля (без трекера)",
                "total_km": 0, "run_count": 0,
                "avg_pace": "—", "avg_hr": None, "fatigue_level": "unknown",
                "vo2max": _profile["vo2max"],
                "vo2max_source": _profile.get("vo2max_source") or "профиль",
            }
            if _profile.get("lactate_threshold_pace"):
                fitness["lactate_threshold_pace"] = _profile["lactate_threshold_pace"]
            if _profile.get("lactate_threshold_hr"):
                fitness["lactate_threshold_hr"] = _profile["lactate_threshold_hr"]
            if _profile.get("gender"):
                fitness["gender"] = _profile["gender"]
            logger.info(f"Источник данных: profile_only (fast mode) для user {db_user_id}")
        else:
            logger.info(f"Источник данных: нет данных для user {db_user_id}")
            text = "🕐 Long Run найден!\n\nПодключи Garmin (/connect_garmin), COROS (/connect_coros) или Polar (/connect_polar) для рекомендации группы."
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
                fitness["vo2max_source"] = source or "профиль"
        if profile.get("lactate_threshold_pace"):
            fitness["lactate_threshold_pace"] = profile["lactate_threshold_pace"]
        if profile.get("lactate_threshold_hr"):
            fitness["lactate_threshold_hr"] = profile["lactate_threshold_hr"]
        if profile.get("gender"):
            fitness["gender"] = profile["gender"]

    # Данные восстановления — всегда свежие для /long
    recovery = await _get_recovery_data(db_user_id, force_fresh=True)

    # Погода
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
        ai_mode = "fast"  # profile-only → принудительно fast
    wait_msg = {"deep": "🧠 Думаю над рекомендацией... (~2-3 минуты)", "smart": "⚡ Анализирую... (~1-2 минуты)", "fast": "🔥 Считаю быстро... (~30 секунд)"}.get(ai_mode, "⚡ Анализирую... (~1-2 минуты)")
    if msg:
        await msg.edit_text(wait_msg)

    prompt = build_long_run_prompt(workout, fitness, recovery, weather_prompt=weather_prompt)
    import functools
    result = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(ask_groq, prompt, ai_mode))
    if result and result.get("timeout"):
        timeout_text = "⏱ Модель думает слишком долго. Попробуй ⚡ Умный режим (/mode)"
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
            InlineKeyboardButton("📥 Скачать JSON", callback_data="fit_dl"),
            InlineKeyboardButton("⌚ Загрузить в Garmin", callback_data="fit_up"),
        ]])
        rating_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("⭐ Оценить рекомендацию", callback_data="rate_show"),
        ]])

    final_markup = _merge_keyboards(fit_markup, rating_markup, get_main_keyboard(from_recommendation=True))
    if msg:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=final_markup)
    else:
        await context.bot.send_message(telegram_id, text, parse_mode="HTML",
                                       reply_markup=get_main_keyboard(from_recommendation=True))


async def _fetch_garmin_recovery(db_user_id: int) -> dict | None:
    """Запрашивает данные восстановления из Garmin API и сохраняет в кэш."""
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
    """Возвращает данные восстановления (Whoop → Garmin).
    force_fresh=True — пропускает кэш Garmin, всегда запрашивает API.
    Используй force_fresh=True для /workout и /long, чтобы TR и Body Battery были актуальными.
    """
    prefs = get_preferences(db_user_id)
    use_garmin = prefs.get("use_garmin_recovery", True) if prefs else True
    has_garmin = bool(get_token(db_user_id, "garmin"))

    # Whoop — приоритет
    whoop_data = None
    from whoop import get_full_recovery_data, ensure_valid_token as whoop_valid_token
    try:
        access_token = await whoop_valid_token(db_user_id)
        if access_token:
            whoop_data = await get_full_recovery_data(access_token)
    except Exception as e:
        logger.error(f"Whoop error for user {db_user_id}: {e}")

    if whoop_data:
        # Если Garmin носят постоянно — Training Readiness поверх Whoop
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

    # Garmin — кэш (8 ч) или живой запрос
    if use_garmin and has_garmin:
        if not force_fresh:
            cached = get_garmin_recovery_cache(db_user_id)
            if cached:
                return cached
        garmin_result = await _fetch_garmin_recovery(db_user_id)
        if garmin_result:
            return garmin_result

    # COROS — третий приоритет
    if get_token(db_user_id, "coros"):
        try:
            import coros as _coros
            result = await _coros.get_recovery_for_prompt(db_user_id)
            if result:
                return result
        except Exception as e:
            logger.error(f"COROS recovery error for user {db_user_id}: {e}")

    # Polar — четвёртый приоритет
    if get_token(db_user_id, "polar"):
        try:
            import polar as _polar
            result = await _polar.get_recovery_for_prompt(db_user_id)
            if result:
                return result
        except Exception as e:
            logger.error(f"Polar recovery error for user {db_user_id}: {e}")

    return None


# ── ПРОВЕРКА НОВЫХ АНОНСОВ ───────────────────────────────────

async def _notify_all(context, text: str, notify_key: str = "") -> int:
    """Рассылает текст активным пользователям с включённым уведомлением. Возвращает количество успешных."""
    users = get_users_for_notification(notify_key) if notify_key else get_active_users()
    count = 0
    for telegram_id, name, _ in users:
        try:
            await context.bot.send_message(telegram_id, text, parse_mode="HTML")
            count += 1
            await asyncio.sleep(0.3)
        except Forbidden:
            _mark_user_inactive(telegram_id)
            logger.info(f"Пользователь {telegram_id} заблокировал бота, отмечен как неактивный")
        except Exception as e:
            logger.error(f"Broadcast error for {telegram_id}: {e}")
    return count


async def _broadcast_split(
    context,
    text_with_data: str,
    text_no_data: str,
    notify_key: str = "",
) -> int:
    """Рассылка с разным текстом: полная версия для пользователей с данными, упрощённая — без."""
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
            logger.info(f"Пользователь {telegram_id} заблокировал бота, отмечен как неактивный")
        except Exception as e:
            logger.error(f"Broadcast error for {telegram_id}: {e}")
    return count


def _edit_newer(a: str | None, b: str | None) -> bool:
    """True если edit_date a новее b (оба ISO-строки или None)."""
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
    """Фоновый автоанализ анонса (Шаг 1) → запись в workout_analysis.
    Запускается при: новом анонсе / новой доп. группе / редактировании поста.
    Прод-режим (get_preprocess_mode). Не блокирует цикл проверки.
    После успешного анализа уведомляет ТОЛЬКО админа (контроль, что бот поймал анонс).
    Пользователям ничего не шлёт — их единственное сообщение это вечерняя рассылка 20:00.
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
            reason = "новый анонс"
        elif _edit_newer(edit_date, existing.get("edit_date")):
            reason = "пост отредактирован"
        else:
            old_extra = _json.loads(existing.get("extra_groups_json") or "[]")
            old_nums = {str(g.get("number")) for g in old_extra}
            new_nums = {str(g.get("number")) for g in extra}
            if new_nums - old_nums:
                reason = "новые доп. группы"
        if not reason:
            return

        mode = get_preprocess_mode()
        logger.info(f"autoanalyze: post_id={post_id} запуск анализа ({reason}, режим {mode})")
        result = await asyncio.get_event_loop().run_in_executor(
            None, functools.partial(analyze_workout, raw_text, comments_text, mode)
        )
        if not result:
            logger.warning(f"autoanalyze: post_id={post_id} анализ не удался ({reason})")
            return
        # Failsafe B: анонс без групп физически бесполезен для рекомендации
        if result.get("is_valid") and not (result.get("groups") or []):
            result["is_valid"] = False
            result["reject_reason"] = "нет групп с темпами — не анонс"
            logger.info(f"autoanalyze: post_id={post_id} is_valid сброшен (groups=[])")
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
            f"autoanalyze: post_id={post_id} сохранён ({reason}) — "
            f"type={result.get('workout_type')}, valid={result.get('is_valid')}, "
            f"groups={n_groups}, extra={n_extra}"
        )

        # Запись для /status («последний анонс»)
        try:
            save_workout_notification(post_id, result.get("workout_type", ""),
                                      result.get("workout_date", ""), [], 0)
        except Exception as e:
            logger.warning(f"autoanalyze: save_workout_notification error: {e}")

        # Уведомление ТОЛЬКО админу — контроль, что анонс пойман и разобран
        if context is not None:
            valid_mark = "✅ валидный" if result.get("is_valid") else "❌ невалидный"
            await _notify_admin(
                context.bot,
                f"🔬 Анонс пойман и проанализирован ({reason})\n"
                f"Тип: {result.get('workout_type', '—')} | Дата: {result.get('workout_date', '—')}\n"
                f"{valid_mark} | групп: {n_groups}, доп.групп: {n_extra} | режим {mode}"
            )
    except Exception as e:
        logger.error(f"autoanalyze error for post {workout.get('post_id')}: {e}")


async def scheduled_new_workout_check(context: ContextTypes.DEFAULT_TYPE):
    """Каждые 30 минут ловит новые/изменённые анонсы и запускает фоновый автоанализ (Шаг 1).
    Пользователям НИЧЕГО не шлёт (никакого промежуточного «вышел анонс») — их единственное
    сообщение про тренировку это вечерняя рассылка 20:00 с готовой рекомендацией.
    Уведомление о поимке+анализе уходит ТОЛЬКО админу (из _autoanalyze_post).
    """
    workout = await find_next_workout()
    if workout and workout.get("post_id"):
        asyncio.create_task(_autoanalyze_post(workout, context))

    workout_lr = await find_next_long_run()
    if workout_lr and workout_lr.get("post_id"):
        asyncio.create_task(_autoanalyze_post(workout_lr, context))


# ── ПЛАНИРОВЩИК ──────────────────────────────────────────────

async def scheduled_evening(context: ContextTypes.DEFAULT_TYPE):
    # return  # TEMP: рассылка отключена 2026-06-01, убрать после фикса зон
    now = datetime.now()
    if now.weekday() not in [0, 3, 5]:
        return
    is_long = (now.weekday() == 5)  # сб → анонс воскресного Long Run
    wtype = "long" if is_long else "interval"
    logger.info(f"Запускаю вечернюю рассылку ({wtype})...")

    # Один раз детектим свежесть анонса (find_next), кэш — источник рекомендации
    live = await (find_next_long_run() if is_long else find_next_workout())
    cur_post = live.get("post_id") if live else None
    cur_edit = live.get("edit_date") if live else None
    _, status = get_latest_workout_analysis(
        wtype, cur_post, live.get("workout_date") if live else None, cur_edit)
    if status == "empty":
        logger.info(f"Вечерняя рассылка: нет анализа в кэше ({wtype}) — пропуск")
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
            logger.info(f"Пользователь {telegram_id} заблокировал бота (вечерняя рассылка)")
        except Exception as e:
            logger.error(f"Evening notification error for {telegram_id}: {e}")
    logger.info(f"Вечерняя рассылка завершена ({wtype}, status={status}): {count} отправлено (кэш, без парсинга на лету)")
    await _notify_admin(
        context.bot,
        f"📨 Рассылка завершена\n"
        f"Тип: {wtype} | Отправлено: {count} пользователям"
    )

async def _get_vo2max_from_tracker(db_user_id: int) -> tuple:
    """Получает VO2max из первого доступного трекера (Garmin → COROS → Polar).
    Возвращает (vo2max: float, tracker_key: str, tracker_name: str) или (None, None, None).
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
    """03:45 UTC (06:45 МСК) — обновляет кэш всех сервисов перед утренней рассылкой.
    Порядок: до scheduled_morning (04:00 UTC / 07:00 МСК).
    Обновляет: Strava CTL/ATL/TSB, Garmin recovery+VO2max, COROS, Polar.
    """
    logger.info("Запускаю обновление кэша всех сервисов (03:45 UTC)...")
    users = get_all_users()
    counts = {"strava": 0, "garmin": 0, "coros": 0, "polar": 0, "vo2max": 0}

    for telegram_id, name, _ in users:
        db_user_id = get_or_create_user(telegram_id, name)

        # ── Strava CTL/ATL/TSB ────────────────────────────────
        try:
            access_token = await ensure_valid_token(db_user_id)
            if access_token:
                await refresh_athlete_cache(db_user_id, access_token)
                counts["strava"] += 1
        except Exception as e:
            logger.warning(f"Strava cache error for {telegram_id}: {e}")

        # ── Garmin: recovery (Body Battery, HRV, TR) ──────────
        if get_token(db_user_id, "garmin"):
            try:
                result = await _fetch_garmin_recovery(db_user_id)
                if result:
                    counts["garmin"] += 1
            except Exception as e:
                logger.warning(f"Garmin recovery error for {telegram_id}: {e}")

        # ── COROS ─────────────────────────────────────────────
        if get_token(db_user_id, "coros"):
            try:
                import coros as _coros
                await _coros.get_full_data(db_user_id)
                counts["coros"] += 1
            except Exception as e:
                logger.warning(f"COROS refresh error for {telegram_id}: {e}")

        # ── Polar ─────────────────────────────────────────────
        if get_token(db_user_id, "polar"):
            try:
                import polar as _polar
                await _polar.get_full_data(db_user_id)
                counts["polar"] += 1
            except Exception as e:
                logger.warning(f"Polar refresh error for {telegram_id}: {e}")

        # ── VO2max из трекера (тихо) ───────────────────────────
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
                        f"VO2max обновлён для {telegram_id}: "
                        f"{float(old_vo2max):.0f} → {new_vo2max:.0f} ({tracker_name})"
                    )

        # ── Персональные темповые зоны (пересчёт после обновления данных) ──
        try:
            zones.recalculate_and_save(db_user_id)
        except Exception as e:
            logger.warning(f"Zones recalc error for {telegram_id}: {e}")

        await asyncio.sleep(1)

    logger.info(
        f"Кэш обновлён: Strava={counts['strava']}, Garmin={counts['garmin']}, "
        f"COROS={counts['coros']}, Polar={counts['polar']}, VO2max изменён={counts['vo2max']}"
    )


async def scheduled_data_refresh(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневно в 03:00 UTC обновляет Strava и Garmin для всех пользователей."""
    logger.info("Запускаю плановое обновление данных (Strava + Garmin)...")
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

            # VO2max — обновляем если >7 дней
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
                        if not (profile or {}).get("vo2max_locked"):
                            save_user_profile(db_user_id, vo2max=vo2max, vo2max_source="garmin")
                            vo2max_ok += 1
                    if not isinstance(lt, Exception) and lt:
                        if not (profile or {}).get("lactate_locked"):
                            save_user_profile(db_user_id,
                                              lactate_threshold_pace=lt["pace"],
                                              lactate_threshold_hr=lt["hr"],
                                              lactate_source="auto")
            except Exception as e:
                logger.error(f"Garmin VO2max refresh error for {telegram_id}: {e}")

        await asyncio.sleep(1)

    logger.info(f"Обновление завершено: Strava={strava_ok}, Garmin recovery={garmin_ok}, VO2max={vo2max_ok}")


async def scheduled_morning(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    if now.weekday() not in [1, 4, 6]:
        return
    logger.info("Запускаю утреннюю рассылку...")
    # Пользователей без профиля/трекера не беспокоим — им нечего показывать
    users = [(tid, name, un) for tid, name, un, has in get_all_users_with_status() if has]
    for telegram_id, name, _ in users:
        try:
            await _send_morning_check(telegram_id, context)
            await asyncio.sleep(0.5)
        except Forbidden:
            _mark_user_inactive(telegram_id)
            logger.info(f"Пользователь {telegram_id} заблокировал бота (утренняя рассылка)")
        except Exception as e:
            logger.error(f"Morning notification error for {telegram_id}: {e}")


# ── ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ─────────────────────────────

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит все необработанные исключения из хендлеров."""
    error = context.error

    # Повторное нажатие кнопки — сообщение уже не изменилось, игнорируем
    if isinstance(error, BadRequest) and "Message is not modified" in str(error):
        return

    # Таймауты и сетевые ошибки — просто логируем на уровне warning
    if isinstance(error, (TimedOut, NetworkError)):
        logger.warning(f"Network error: {error}")
        return

    # Всё остальное — логируем полностью
    logger.error("Необработанное исключение:", exc_info=error)

    # Уведомляем пользователя
    if update and hasattr(update, 'effective_chat') and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Что-то пошло не так. Попробуй ещё раз или переключи режим AI (/mode)"
            )
        except Exception:
            pass


# ── ЗАПУСК ───────────────────────────────────────────────────

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
    app.add_handler(CommandHandler("show_analyze",  cmd_show_analyze))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(global_error_handler)

    job_queue = app.job_queue
    job_queue.run_daily(scheduled_evening,       time=time(hour=17, minute=0))           # 20:00 МСК
    job_queue.run_daily(scheduled_cache_refresh, time=time(hour=3,  minute=45))          # 06:45 МСК — все сервисы
    job_queue.run_daily(scheduled_morning,       time=time(hour=4,  minute=0))           # 07:00 МСК — после кэша
    job_queue.run_repeating(scheduled_new_workout_check, interval=1800, first=60)        # каждые 30 мин

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

        # Единый Telethon-клиент на процесс (прогрев)
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
                logger.info("✅ Бот запущен!")
                try:
                    await stop_event.wait()
                finally:
                    await app.updater.stop()
                    await app.stop()
                logger.info("Бот остановлен")
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