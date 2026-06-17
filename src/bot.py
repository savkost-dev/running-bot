import os
import asyncio
import logging
from datetime import datetime, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TimedOut, NetworkError, Forbidden
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, TypeHandler
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
    save_last_recommendation, get_last_recommendation, get_recommendations_for_date,
    get_workout_notification, save_workout_notification, get_last_workout_notification,
    get_users_for_notification,
    get_garmin_recovery_cache, save_garmin_recovery_cache,
    user_exists, log_activity, get_bot_stats, count_users_with_service,
    get_activity_daily, get_activity_top, get_activity_users,
    delete_token,
    get_all_users_with_details, get_users_with_service_full, get_users_with_profile_full,
    save_feedback, save_rating, get_recent_ratings, get_recent_feedbacks,
    save_workout_analysis, get_workout_analysis, get_latest_workout_analysis,
    get_preprocess_mode, set_preprocess_mode,
    get_users_list_for_b,
    get_morning_caught,
    save_workout_template,
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
from recovery import (
    _update_garmin_recovery_from_raw, _fetch_garmin_recovery,
    _get_recovery_data, _garmin_observation_end,
    _get_unified_recovery, _recovery_scenario,
)
from fitness import (
    refresh_athlete_cache, get_fitness_data,
    get_garmin_fitness_data, get_coros_fitness_data, get_polar_fitness_data,
    _get_vo2max_from_tracker,
)

ADMIN_TELEGRAM_IDS = {273726778}
ADMIN_ID = 273726778

# Поимённая секция в отчёте админу после вечерней рассылки.
# Когда юзеров станет много — выключить (останется только сводка по группам).
BROADCAST_REPORT_DETAILED = True


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


def _add_main_menu_btn(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    """Добавляет кнопку Главное меню если её ещё нет в markup."""
    existing = list(markup.inline_keyboard) if markup else []
    for row in existing:
        for btn in row:
            if getattr(btn, "callback_data", None) in ("main_menu", "main_menu_new"):
                return markup  # уже есть
    menu_row = [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu_new")]
    return InlineKeyboardMarkup(existing + [menu_row])


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
    msg = await update.message.reply_text("🔍 Ищу анонс, анализирую и подбираю группу...")
    await _send_recommendation(user.id, user.full_name, context, long=False, msg=msg)


async def cmd_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    msg = await update.message.reply_text("🔍 Подбираю Long Run...")
    await _send_recommendation(user.id, user.full_name, context, long=True, msg=msg)


async def cmd_morning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
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
            f"Обновлено: {updated_at}",
            reply_markup=_add_main_menu_btn(None),
        )
    else:
        await msg.edit_text("❌ Не удалось обновить данные. Попробуй позже.",
                            reply_markup=_add_main_menu_btn(None))


def _workout_is_past(workout_date: str, schedule: str = "") -> bool:
    """True если тренировка уже прошла (cutoff 09:00 МСК)."""
    from datetime import datetime, timezone, timedelta
    import re as _re
    MSK = timezone(timedelta(hours=3))
    m = _re.search(r'(\d{1,2}:\d{2})', schedule or "")
    start_time = m.group(1) if m else "07:00"
    try:
        wdt = datetime.strptime(f"{workout_date} {start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
        return datetime.now(MSK) > wdt.replace(hour=9, minute=0, second=0, microsecond=0)
    except Exception:
        return False


def _extract_group_pace(grp: dict) -> tuple:
    """Возвращает (pace_start, pace_end, progression) для группы.
    Работает и для лонга (прямые поля) и для интервальных (через blocks).
    """
    ps   = grp.get("pace_start")
    pe   = grp.get("pace_end")
    prog = grp.get("progression")
    if not ps:
        blocks = grp.get("blocks") or []
        work_blocks = [b for b in blocks
                       if b.get("type") == "work"
                       or b.get("work_pace_start")
                       or b.get("pace_start")]
        if work_blocks:
            ps = (work_blocks[0].get("work_pace_start")
                  or work_blocks[0].get("pace_start"))
            pe = (work_blocks[-1].get("work_pace_end")
                  or work_blocks[-1].get("pace_end")
                  or work_blocks[-1].get("work_pace_start")
                  or work_blocks[-1].get("pace_start"))
            if not prog and len(work_blocks) > 1 and ps and pe and ps != pe:
                prog = True
    return ps, pe, prog


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
        "\nДля анализа заполни профиль, а для расширенного подключи один из трекеров:\n"
        "👤 /profile — VO2max и лактатный порог\n"
        "🔗 /connect_garmin, /connect_coros, /connect_polar, /connect_strava"
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
        reply_markup=_add_main_menu_btn(_build_mode_keyboard(current_mode))
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
            "/show_analyze — показать последний Шаг 1 из базы\n"
            "/b — вариант B для себя\n"
            "/b_user — вариант B для выбранного пользователя\n"
            "/a_user — вариант A для выбранного пользователя\n"
            "/w_user — реальный путь пользователя (его ai_mode: B или A)\n"
            "/l_user — лонг для выбранного пользователя\n"
            "/p_b — промпт варианта B для себя\n"
            "/p_b_user — промпт варианта B для выбранного пользователя\n"
            "/p_a — промпт варианта A для себя\n"
            "/p_a_user — промпт варианта A для выбранного пользователя\n"
            "/p_analyze — промпт Шага 1 (анализ анонса)\n"
            "/activity — активность по дням и топ действий за 14 дней"
            "\n/msg_user <id> <текст> — написать юзеру от имени бота"
            "\n/last — разбор последней выполненной тренировки (графики факт vs план; /last dark — тёмная тема)"
            "\n/ai — ИИ-анализ последней тренировки (/ai DD_20260612 — выбрать; /ai data — сырой пакет+промпт)"
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
            reply_markup=_add_main_menu_btn(InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, отключить", callback_data=f"disc_yes_{svc}"),
                InlineKeyboardButton("❌ Отмена",        callback_data="svc_cancel"),
            ]]))
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
            reply_markup=_add_main_menu_btn(InlineKeyboardMarkup([
                [InlineKeyboardButton("👨 Мужской", callback_data="profile_gender_male"),
                 InlineKeyboardButton("👩 Женский", callback_data="profile_gender_female")],
            ]))
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
            await context.bot.send_message(user.id, f"❌ Ошибка генерации JSON: {type(e).__name__}: {e}",
                reply_markup=_add_main_menu_btn(None))

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
                    parse_mode="HTML",
                    reply_markup=_add_main_menu_btn(None))
            else:
                await context.bot.send_message(user.id,
                    "❌ Не удалось загрузить в Garmin Connect.\n\n"
                    "Попробуй скачать JSON кнопкой 📥 и импортировать вручную.",
                    reply_markup=_add_main_menu_btn(None))
        except Exception as e:
            logger.error(f"Garmin upload error for {user.id}: {e}")
            await context.bot.send_message(user.id,
                f"❌ Ошибка загрузки в Garmin: {type(e).__name__}",
                reply_markup=_add_main_menu_btn(None))

    elif query.data.startswith("garmin_grp_"):
        group_num = query.data[len("garmin_grp_"):]
        data = _fit_data.get(user.id)
        if not data:
            await context.bot.send_message(
                user.id,
                "⏱ Данные устарели. Запроси рекомендацию заново (/workout).",
                reply_markup=_add_main_menu_btn(None),
            )
            return
        try:
            from fit_generator import build_garmin_from_analysis, workout_filename
            analysis_d = data.get("analysis") or {}
            wdate = analysis_d.get("workout_date", "")
            wkt = build_garmin_from_analysis(analysis_d, group_num)
            fname = workout_filename(wdate, group_num)
            _fit_data[user.id] = {
                **data,
                "recommended_group": group_num,
                "garmin_json": wkt,
            }
            await context.bot.send_message(
                user.id,
                f"⌚ <b>{fname}</b>\n\nСкачай JSON или загрузи напрямую в Garmin Connect:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📥 Скачать JSON", callback_data="fit_dl"),
                    InlineKeyboardButton("⌚ Загрузить в Garmin", callback_data="fit_up"),
                ]]),
            )
        except Exception as e:
            logger.error(f"garmin_grp_{group_num} error for {user.id}: {e}", exc_info=True)
            await context.bot.send_message(
                user.id,
                f"❌ Ошибка генерации тренировки: {type(e).__name__}: {e}",
                reply_markup=_add_main_menu_btn(None),
            )

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

    # ── /msg_user: текст юзеру от имени бота (admin) ──
    elif context.user_data.get("awaiting_msg_user"):
        target = context.user_data.pop("awaiting_msg_user")
        if text.strip().lower() in ("отмена", "cancel", "/cancel"):
            await update.message.reply_text("Отменено, ничего не отправлено.")
            return
        try:
            await context.bot.send_message(target["tg_id"], text)
            await update.message.reply_text(f"✅ Отправлено: {target['name']}")
        except Exception as e:
            await update.message.reply_text(
                f"❌ Не отправилось для {target['name']}: {type(e).__name__}: {e}")
        return
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


async def _send_admin_data_block(
    telegram_id: int,
    db_user_id: int,
    recovery: dict | None,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Админу — отдельным сообщением: снимок на утро (из базы) + текущие данные (на лету).
    Вызывается в конце каждой рекомендации (A и B). Только для админа.
    """
    if telegram_id not in ADMIN_TELEGRAM_IDS:
        return
    try:
        _snap = get_morning_caught(db_user_id)
        _rec = recovery or {}
        _tr_cur = (_rec.get("training_readiness") or {}).get("score") if isinstance(_rec.get("training_readiness"), dict) else None
        _lines = ["🔬 <b>Данные для рекомендации</b>"]
        if _snap and _snap.get("caught"):
            # snapshot_at (когда бот снял снимок), UTC → МСК, только время
            _snap_t = ""
            _sa = _snap.get("snapshot_at")
            if _sa:
                try:
                    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                    _sd = _dt.fromisoformat(str(_sa).replace("Z", "+00:00"))
                    if _sd.tzinfo is None:
                        _sd = _sd.replace(tzinfo=_tz.utc)
                    _snap_t = ", снят " + _sd.astimezone(_tz(_td(hours=3))).strftime("%H:%M МСК")
                except Exception:
                    pass
            _lines.append(
                f"\n<b>Снимок на утро</b> ({_snap.get('date') or '—'}{_snap_t}):\n"
                f"TR {_snap.get('tr')} | BB {_snap.get('bb')} | HRV {_snap.get('hrv')} | "
                f"RHR {_snap.get('rhr')} | сон {_snap.get('sleep_h')}ч | подъём {(_snap.get('wake_at') or '—')[11:16]}"
            )
        else:
            _lines.append("\n<b>Снимок на утро</b>: нет (ночь не поймана)")
        # data_fetched_at: живой синк = GMT с 'Z' → конвертируем в МСК; naive без tz = уже локальное
        _dt_cur = ""
        _raw_dt = str(_rec.get("data_fetched_at") or "")
        if _raw_dt:
            try:
                from datetime import datetime as _dtm, timezone as _tzm, timedelta as _tdm
                _d = _dtm.fromisoformat(_raw_dt.replace("Z", "+00:00"))
                _d = _d.astimezone(_tzm(_tdm(hours=3))) if _d.tzinfo else _d
                _dt_cur = _d.strftime("%Y-%m-%d %H:%M")
            except Exception:
                _dt_cur = (_raw_dt[:16]).replace("T", " ")
        _bb_cur = (_rec.get("recovery_score") if _rec.get("source") == "unified_cache"
                   else _rec.get("body_battery"))
        _lines.append(
            f"\n<b>Последняя синхронизация</b> ({_dt_cur or '—'}):\n"
            f"TR {_tr_cur} | BB {_bb_cur}"
        )
        await context.bot.send_message(telegram_id, "\n".join(_lines), parse_mode="HTML")
    except Exception as _e:
        logger.warning(f"admin snapshot block: {_e}")


async def _send_ai_variant_b(
    telegram_id: int,
    analysis: dict,
    user_data: dict,
    context: ContextTypes.DEFAULT_TYPE,
    workout_dict: dict | None = None,
    weather_line: str = "",
    msg=None,
    is_broadcast: bool = False,
) -> None:
    """Вариант B: ИИ сам выбирает группу.
    Для deep/fast/smart — основной путь рекомендации (вместо A).
    Для /b и /test_workout — вызывается через create_task (админ).
    """
    import functools
    db_user_id = user_data.get("db_user_id")
    rec_mode = (get_preferences(db_user_id) or {}).get("ai_mode", "smart")

    prompt, scenario_ctx = await _build_variant_b_prompt(
        db_user_id, analysis, user_data, workout_dict
    )

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            functools.partial(claude_advisor.ask_groq, prompt, rec_mode)
        )
        if not result or not result.get("advice"):
            logger.warning("_send_ai_variant_b: ask_groq returned no advice")
            return
        advice = result["advice"]
        stats = result.get("stats", {})
        # Поля из анализа (Шаг 1) — подставляем в коде, ИИ не дублирует
        spec = (get_preferences(db_user_id) or {}).get("specialization") or "half_marathon"
        advice["overall_purpose"] = analysis.get("overall_purpose", "")
        advice["workout_summary"] = analysis.get("summary", "")
        advice["spec_label"] = claude_advisor._SPEC_LABELS.get(spec, spec)
        # Санитайз номеров групп
        for item in (advice.get("suitability_percentages") or []):
            if "group" in item:
                item["group"] = claude_advisor._sanitize_group_name(str(item["group"]))
        # Добираем числовые группы которые ИИ не включил в suitability
        # (группу здоровья пропускаем — она не оценивается процентом)
        _suit = advice.get("suitability_percentages") or []
        _suit_groups = {str(s.get("group", "")) for s in _suit}
        for g in (analysis.get("groups") or []):
            gnum = claude_advisor._sanitize_group_name(str(g.get("number", "")))
            if gnum and gnum not in _suit_groups and any(c.isdigit() for c in gnum):
                _suit.append({"group": gnum, "percentage": 0, "comment": "risk"})
        advice["suitability_percentages"] = _suit
        # Шапка/работа/погода — те же, что у варианта A (полный workout_dict).
        # Фолбэк на минимальный dict, если вызвали без него.
        if workout_dict:
            workout_for_render = dict(workout_dict)
        else:
            workout_for_render = {
                "workout_type": analysis.get("workout_type", "interval"),
                "workout_date": analysis.get("workout_date", ""),
                "location": analysis.get("location", ""),
                "schedule": analysis.get("schedule", ""),
                "work_text": analysis.get("work_text", ""),
            }
        msg_text = claude_advisor.format_evening_message(
            advice, workout_for_render, stats, weather_line=weather_line
        )
        msg_text = scenario_ctx["user_text"] + "\n\n" + msg_text
        _rating_data[telegram_id] = {
            "workout_date": analysis.get("workout_date", ""),
            "ai_mode": rec_mode,
        }
        rating_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("⭐ Оценить рекомендацию", callback_data="rate_show"),
        ]])
        final_b_markup = _merge_keyboards(rating_markup, get_main_keyboard(from_recommendation=True))
        # Сохранять для утренней — только при плановой рассылке (is_broadcast=True)
        if is_broadcast:
            try:
                _eve_rec = claude_advisor._recovery_value(user_data.get("recovery"))
                _eve_rs = int(_eve_rec) if _eve_rec is not None else None
                _lowered = bool(advice.get("lowered_by_recovery"))
                save_last_recommendation(
                    db_user_id, advice, workout_for_render, ai_mode=rec_mode,
                    evening_recovery_score=_eve_rs,
                    lowered_by_recovery=_lowered,
                )
            except Exception as _e:
                logger.error(f"save_last_recommendation (B): {_e}")
        # Админу — снимок на утро (из базы) + текущие данные (на лету), отдельным сообщением
        await _send_admin_data_block(telegram_id, db_user_id, user_data.get("recovery"), context)
        if msg:
            try:
                await msg.edit_text(msg_text, parse_mode="HTML", reply_markup=final_b_markup)
            except Exception:
                await context.bot.send_message(telegram_id, msg_text, parse_mode="HTML", reply_markup=final_b_markup)
        else:
            await context.bot.send_message(telegram_id, msg_text, parse_mode="HTML", reply_markup=final_b_markup)

        # Garmin export — топ-3 группы для выбора
        _suit_all = advice.get("suitability_percentages") or []
        _top3 = sorted(
            [s for s in _suit_all
             if s.get("percentage", 0) > 0
             and any(c.isdigit() for c in str(s.get("group", "")))],
            key=lambda x: x.get("percentage", 0),
            reverse=True,
        )[:3]
        # Отдельное сообщение с выгрузкой в Garmin — только если Garmin подключён.
        if _top3 and get_token(db_user_id, "garmin"):
            _fit_data[telegram_id] = {
                "type": "b_interval",
                "analysis": analysis,
                "workout": workout_for_render,
                "recommended_group": str(advice.get("recommended_group", "")),
            }
            _garmin_btns = [
                InlineKeyboardButton(
                    f"⌚ Гр.{s['group']} ({s['percentage']}%)",
                    callback_data=f"garmin_grp_{s['group']}",
                )
                for s in _top3
            ]
            await context.bot.send_message(
                telegram_id,
                "🏃 Загрузить тренировку в Garmin — выбери группу:",
                reply_markup=InlineKeyboardMarkup([_garmin_btns]),
            )

        # Второе сообщение — свободный текст от ИИ (нюансы, физиология)
        # Временно отключено
        # extra = await asyncio.get_event_loop().run_in_executor(
        #     None,
        #     functools.partial(
        #         claude_advisor.generate_ai_b_extra, analysis, advice, rec_mode
        #     )
        # )
        # if extra:
        #     await context.bot.send_message(
        #         telegram_id,
        #         f"🧪 <b>Дополнение B</b>\n\n{extra}",
        #         parse_mode="HTML"
        #     )
        # else:
        #     logger.warning("_send_ai_variant_b: extra (B2) empty — пропускаю второе сообщение")
    except Exception as e:
        logger.error(f"_send_ai_variant_b error: {e}")


async def _send_recommendation(
    telegram_id: int, name: str,
    context: ContextTypes.DEFAULT_TYPE,
    long: bool = False,
    msg=None,
    live: dict | None = None,
    is_broadcast: bool = False,
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
            simple = (
                f"📢 Тренировка {row.get('workout_date', '')}\n\n"
                "Для анализа заполни профиль, а для расширенного подключи один из трекеров."
            )
        no_data_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Профиль",      callback_data="my_profile"),
             InlineKeyboardButton("🔗 Сервисы →",    callback_data="show_services")],
            [InlineKeyboardButton("🏠 Главное меню",  callback_data="main_menu_new")],
        ])
        await _out(banner + simple, markup=no_data_markup)
        return

    # has_data=True → персональная рекомендация из кэша
    try:
        analysis = _json.loads(row.get("analyzed_json") or "{}")
    except Exception:
        analysis = {}

    # is_past по реальному времени ДО запроса recovery (определяет force_fresh)
    _wd = (live or {}).get("workout_date", "") or analysis.get("workout_date", "")
    _sched = (live or {}).get("schedule", "") or analysis.get("schedule", "") or ""
    _is_past_rt = _workout_is_past(_wd, _sched)

    user_data = {
        "db_user_id": db_user_id,
        "specialization": (get_user_profile(db_user_id) or {}).get("specialization"),
        "recovery": await _get_unified_recovery(db_user_id, force_fresh=not _is_past_rt),
    }

    rec_mode = (get_preferences(db_user_id) or {}).get("ai_mode", "smart")
    if rec_mode != "calc" and not long:
        # Путь B — ИИ выбирает группу (deep/smart/fast)
        workout_dict_b = dict(live) if live else {"workout_date": analysis.get("workout_date", "")}
        workout_dict_b["workout_type"] = "interval"
        workout_dict_b["is_past"] = _is_past_rt
        workout_dict_b["even_pace_available"] = analysis.get("even_pace_available")
        weather_b = await get_weather_for_workout(
            workout_dict_b.get("location", ""), workout_dict_b.get("workout_date", ""),
            workout_dict_b.get("schedule", ""),
        )
        weather_line_b = format_weather_for_message(weather_b) if weather_b else ""
        if msg:
            try:
                await msg.edit_text("🧪 ИИ анализирует тренировку и подбирает группу... (до 1-2 мин)")
            except Exception:
                pass
        await _send_ai_variant_b(telegram_id, analysis, user_data, context,
                                 workout_dict=workout_dict_b, weather_line=weather_line_b,
                                 msg=msg, is_broadcast=is_broadcast)
        return

    # Для long: жара влияет на ВЫБОР группы. Погоду берём ДО recommend_long
    # (прогноз на старт + через 2 ч); тот же объект переиспользуем ниже.
    weather_long = None
    if long:
        weather_long = await get_weather_for_workout(
            (live or {}).get("location", "") or analysis.get("location", ""),
            (live or {}).get("workout_date", "") or analysis.get("workout_date", ""),
            (live or {}).get("schedule", "") or analysis.get("schedule", "") or "",
        )
    _temps = [t for t in ((weather_long or {}).get("temp"),
                          (weather_long or {}).get("temp_plus2h")) if t is not None]
    _temp_c = max(_temps) if _temps else None

    rec = (claude_advisor.recommend_long(analysis, user_data, temp_c=_temp_c) if long
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
    workout_dict["is_past"] = _is_past_rt  # по реальному времени, не по status
    workout_dict["even_pace_available"] = analysis.get("even_pace_available")

    # Сценарий восстановления (время данных, шапка)
    scenario_ctx = _recovery_scenario(
        workout_dict,
        (user_data["recovery"] or {}).get("data_fetched_at") if user_data["recovery"] else None,
    )
    # Для long погода уже получена выше (weather_long) — переиспользуем.
    weather = weather_long if long else await get_weather_for_workout(
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

    # Поля прогрессии рекомендованной группы — для заголовка и промпта
    _adv_grp_num = str(advice.get("recommended_group") or "")
    _adv_grp = next(
        (g for g in analysis.get("groups", []) if str(g.get("number", "")) == _adv_grp_num),
        {},
    )
    advice["rec_group_pace_start"], advice["rec_group_pace_end"], \
        advice["rec_group_progression"] = _extract_group_pace(_adv_grp)

    # Режим рекомендации (Шаг 2) из настроек пользователя; анализ (Шаг 1) всегда deep
    rec_mode = (get_preferences(db_user_id) or {}).get("ai_mode", "smart")
    main = rec.get("main_group") or {}
    _profile = get_user_profile(db_user_id) or {}
    _rec_group_num = str(advice.get("recommended_group") or "")
    _rec_grp = next(
        (g for g in analysis.get("groups", []) if str(g.get("number", "")) == _rec_group_num),
        None,
    )
    _rg_ps, _rg_pe, _rg_prog = _extract_group_pace(_rec_grp or {})
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
        "gender":                _profile.get("gender"),
        "birth_year":            _profile.get("birth_year"),
        "rec_group_pace_start":  _rg_ps,
        "rec_group_pace_end":    _rg_pe,
        "rec_group_progression": _rg_prog,
        "recovery_scenario":     scenario_ctx["prompt_text"],
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

    # Сохранять для утренней — только при плановой рассылке (is_broadcast=True)
    if is_broadcast:
        try:
            _eve_rec = claude_advisor._recovery_value(user_data.get("recovery"))
            _eve_rs = int(_eve_rec) if _eve_rec is not None else None
            save_last_recommendation(
                db_user_id, advice, workout_dict, ai_mode=rec_mode,
                evening_recovery_score=_eve_rs,
                lowered_by_recovery=False,
            )
        except Exception as _e:
            logger.error(f"save_last_recommendation (A): {_e}")
    # Админу — снимок на утро (из базы) + текущие данные (на лету), отдельным сообщением
    await _send_admin_data_block(telegram_id, db_user_id, user_data.get("recovery"), context)
    scenario_header = scenario_ctx["user_text"] + "\n\n" if scenario_ctx.get("user_text") else ""
    await _out(scenario_header + banner + body, final_markup, parse_mode="HTML")




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
        _mm = _add_main_menu_btn(None)
        if msg:
            await msg.edit_text(text, reply_markup=_mm)
        else:
            await context.bot.send_message(telegram_id, text, reply_markup=_mm)
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
        _mm = _add_main_menu_btn(None)
        if msg:
            await msg.edit_text(text, reply_markup=_mm)
        else:
            await context.bot.send_message(telegram_id, text, reply_markup=_mm)
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
        _mm = _add_main_menu_btn(None)
        if msg:
            await msg.edit_text(timeout_text, reply_markup=_mm)
        else:
            await context.bot.send_message(telegram_id, timeout_text, reply_markup=_mm)
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

    # is_past ДО запроса recovery (определяет force_fresh)
    _is_past = _workout_is_past(workout.get("workout_date", ""), workout.get("schedule", ""))
    workout["is_past"] = _is_past

    # Данные восстановления
    recovery = await _get_unified_recovery(db_user_id, force_fresh=not _is_past)

    scenario_ctx = _recovery_scenario(
        workout,
        (recovery or {}).get("data_fetched_at"),
    )

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

    _zones_map = None
    try:
        import zones as _zones_mod
        _zinfo = _zones_mod.get_pace_zones(db_user_id)
        _zones_map = (_zinfo or {}).get("zones")
    except Exception as _e:
        logger.warning(f"long: не удалось получить зоны user={db_user_id}: {_e}")

    prompt = build_long_run_prompt(workout, fitness, recovery, weather_prompt=weather_prompt,
                                   recovery_scenario_text=scenario_ctx["prompt_text"],
                                   zones_map=_zones_map)
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

    scenario_header = scenario_ctx["user_text"] + "\n\n" if scenario_ctx.get("user_text") else ""
    final_markup = _merge_keyboards(fit_markup, rating_markup, get_main_keyboard(from_recommendation=True))
    if msg:
        await msg.edit_text(scenario_header + text, parse_mode="HTML", reply_markup=final_markup)
    else:
        await context.bot.send_message(telegram_id, scenario_header + text, parse_mode="HTML",
                                       reply_markup=get_main_keyboard(from_recommendation=True))


async def _build_variant_b_prompt(
    db_user_id: int,
    analysis: dict,
    user_data: dict,
    workout_dict: dict | None,
) -> tuple[str, dict]:
    """Единая точка сборки промпта варианта B.
    Возвращает (prompt_str, scenario_ctx).
    Используется и /b (_send_ai_variant_b) и /p_b (показ промпта).
    """
    import zones as _z
    from datetime import datetime, timezone, timedelta
    import re as _re_bvp

    zinfo = _z.get_pace_zones(db_user_id) if db_user_id else None
    zones_map = (zinfo or {}).get("zones") or {}

    # is_past по реальному времени (cutoff 09:00 МСК) — ДО запроса recovery
    _is_past = _workout_is_past(
        (workout_dict or {}).get("workout_date", ""),
        (workout_dict or {}).get("schedule", ""),
    )
    if workout_dict is not None:
        workout_dict["is_past"] = _is_past

    # is_past → кэш, будущая → свежие данные
    recovery = await _get_unified_recovery(db_user_id, force_fresh=not _is_past)

    scenario_ctx = _recovery_scenario(
        workout_dict or {},
        (recovery or {}).get("data_fetched_at"),
    )
    # Потолки скорости по скоростным блокам (≤200м, recovery > work или цель — скорость)
    _speed_ceilings = []
    _rep_pace = zones_map.get("repetition")
    _k100 = (zinfo or {}).get("speed_k100")
    if _rep_pace and _k100 is not None:
        _seen_dist: set[int] = set()
        for _b in (analysis.get("structure") or []):
            if _b.get("type") != "repeat":
                continue
            _d = _b.get("work_distance_m") or 0
            _rec = _b.get("recovery_distance_m") or 0
            _purpose = (_b.get("purpose") or "").lower()
            _is_speed = (
                50 <= _d <= 200 and
                (_rec > _d or any(kw in _purpose for kw in ["скорост", "нейромышечн", "нейромышц"]))
            )
            if _is_speed and _d not in _seen_dist:
                _seen_dist.add(_d)
                _c = _z.speed_ceiling_for_distance(_rep_pace, _k100, _d)
                if _c:
                    _speed_ceilings.append({"distance_m": _d, "ceiling": _c})

    prompt = claude_advisor.build_ai_b_prompt(
        analysis, user_data, zones_map, recovery,
        recovery_scenario_text=scenario_ctx["prompt_text"],
        speed_ceilings=_speed_ceilings or None,
    )
    return prompt, scenario_ctx


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
        # Failsafe B: анонс без групп С ТЕМПАМИ физически бесполезен для рекомендации.
        # Прогрев-посты проходят с одной группой «здоровье» без темпа — это не анонс.
        # Группа «с темпом» = в её JSON есть паттерн M:SS (схемонезависимо для interval/long).
        if result.get("is_valid"):
            import re as _re_fs
            _paced = [g for g in (result.get("groups") or [])
                      if _re_fs.search(r"\d{1,2}:\d{2}", _json.dumps(g, ensure_ascii=False))]
            if not _paced:
                result["is_valid"] = False
                result["reject_reason"] = "нет групп с темпами — не анонс"
                logger.info(f"autoanalyze: post_id={post_id} is_valid сброшен (нет групп с темпами)")
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


# ── ДЕТЕКТОР ЗАКОНЧЕННОЙ НОЧИ (опросник пробуждения) ─────────

def _night_services(db_user_id: int) -> list[str]:
    """Ночные сервисы юзера (дают данные за сон). Strava не в счёт."""
    return [s for s in ("garmin", "coros", "polar", "whoop") if get_token(db_user_id, s)]


def _night_ready(db_user_id: int, today_msk: str) -> bool | None:
    """True/False — обработана ли сегодняшняя ночь по сырью. None — нет ночного сервиса.

    Критерий (любой сработавший источник = ночь готова):
      garmin: user_summary.bodyBatteryAtWakeTime не None
      coros:  sleepHrvData.avgSleepHrv > 0
      polar:  nightly_recharge[-1].date == сегодня
      whoop:  recovery.records[0].created_at[:10] == сегодня
    """
    import json as _json
    from database import get_raw_service_data
    svcs = _night_services(db_user_id)
    if not svcs:
        return None

    def _raw(svc):
        row = get_raw_service_data(db_user_id, svc)
        if not row:
            return None
        try:
            return _json.loads(row["raw_json"])
        except Exception:
            return None

    # if "garmin" in svcs:
    #     us = (_raw("garmin") or {}).get("user_summary") or {}
    #     if us.get("bodyBatteryAtWakeTime") is not None:
    #         return True

    if "garmin" in svcs:
        g = _raw("garmin") or {}
        dto = (g.get("sleep_data") or {}).get("dailySleepDTO") or {}
        wake_ms = dto.get("sleepEndTimestampLocal")
        if wake_ms:
            from datetime import datetime as _dt
            wake_date = _dt.utcfromtimestamp(int(wake_ms) / 1000).strftime("%Y-%m-%d")
            if wake_date == today_msk:
                return True
    # if "coros" in svcs:
    #     dash = (_raw("coros") or {}).get("dashboard") or {}
    #     info = ((dash.get("data") or {}).get("summaryInfo")) or {}
    #     hrv = (info.get("sleepHrvData") or {}).get("avgSleepHrv")
    #     if hrv and float(hrv) > 0:
    #         return True
    if "coros" in svcs:
        dash = (_raw("coros") or {}).get("dashboard") or {}
        info = ((dash.get("data") or {}).get("summaryInfo")) or {}
        shd = info.get("sleepHrvData") or {}
        hrv = shd.get("avgSleepHrv")
        happen = str(shd.get("happenDay") or "")        # формат COROS: "20260608"
        today_compact = today_msk.replace("-", "")      # "2026-06-08" → "20260608"
        if hrv and float(hrv) > 0 and happen == today_compact:
            return True
    if "polar" in svcs:
        nr = (_raw("polar") or {}).get("nightly_recharge")
        items = nr if isinstance(nr, list) else ((nr or {}).get("recharges") or (nr or {}).get("items") or [])
        items = [x for x in items if isinstance(x, dict)]
        if items and str(items[-1].get("date")) == today_msk:
            return True
    if "whoop" in svcs:
        recs = ((_raw("whoop") or {}).get("recovery") or {}).get("records") or []
        if recs and str(recs[0].get("created_at", "")[:10]) == today_msk:
            return True
    return False


async def _sync_night_services(db_user_id: int) -> None:
    """Дёргает свежее сырьё (слой 1) по ночным сервисам юзера."""
    if get_token(db_user_id, "garmin"):
        import garmin as _g
        await _g.fetch_raw(db_user_id)
    if get_token(db_user_id, "coros"):
        import coros as _c
        await _c.fetch_raw(db_user_id)
    if get_token(db_user_id, "polar"):
        import polar as _p
        await _p.fetch_raw(db_user_id)
    if get_token(db_user_id, "whoop"):
        import whoop as _w
        await _w.fetch_raw(db_user_id)


def _collect_morning_snapshot(db_user_id: int) -> dict:
    """Собирает снимок «на утро» из сырья ночных сервисов + garmin_recovery_cache.
    Возвращает {tr, bb, hrv, rhr, sleep_h, wake_at, snapshot_at}. Любое поле может быть None.
    Источники по приоритету (первый не-None): garmin → whoop → polar → coros.
    """
    import json as _json
    from datetime import datetime, timezone
    from database import get_raw_service_data, get_garmin_recovery_cache

    def _raw(svc):
        row = get_raw_service_data(db_user_id, svc)
        if not row:
            return None
        try:
            return _json.loads(row["raw_json"])
        except Exception:
            return None

    snap = {"tr": None, "bb": None, "hrv": None, "rhr": None,
            "sleep_h": None, "wake_at": None,
            "snapshot_at": datetime.now(timezone.utc).isoformat()}

    # ── Garmin ──
    if get_token(db_user_id, "garmin"):
        g = _raw("garmin") or {}
        us = g.get("user_summary") or {}
        bb = us.get("bodyBatteryAtWakeTime")
        if bb is not None:
            snap["bb"] = int(bb)
        rhr = us.get("restingHeartRate")
        if rhr:
            snap["rhr"] = int(rhr)
        # Точные сон и пробуждение — из sleep_data.dailySleepDTO (не из ползущего user_summary)
        dto = (g.get("sleep_data") or {}).get("dailySleepDTO") or {}
        slp_secs = dto.get("sleepTimeSeconds")
        if slp_secs:
            snap["sleep_h"] = round(int(slp_secs) / 3600, 2)
        elif us.get("sleepingSeconds"):
            snap["sleep_h"] = round(int(us["sleepingSeconds"]) / 3600, 2)
        wake_ms = dto.get("sleepEndTimestampLocal")
        if wake_ms:
            # Garmin уже сдвинул в локальную зону — берём utcfromtimestamp без повторного сдвига
            from datetime import datetime as _dt
            snap["wake_at"] = _dt.utcfromtimestamp(int(wake_ms) / 1000).isoformat()
        elif us.get("wellnessEndTimeLocal"):
            snap["wake_at"] = str(us["wellnessEndTimeLocal"])
        # TR — из сырья training_readiness: первая запись ПОСЛЕ пробуждения
        # (уверены, что после сна и свежая). Время и пробуждение — локальные.
        tr_raw = g.get("training_readiness")
        tr_list = tr_raw if isinstance(tr_raw, list) else ([tr_raw] if tr_raw else [])
        tr_cands = [t for t in tr_list
                    if isinstance(t, dict) and t.get("score") is not None
                    and t.get("timestampLocal")]
        if tr_cands:
            wake_local = None
            if wake_ms:
                from datetime import datetime as _dt2
                wake_local = _dt2.utcfromtimestamp(int(wake_ms) / 1000)
            def _tr_local(t):
                from datetime import datetime as _dt3
                try:
                    return _dt3.fromisoformat(t["timestampLocal"])
                except Exception:
                    return _dt3.max
            after_wake = ([t for t in tr_cands if _tr_local(t) >= wake_local]
                          if wake_local else [])
            pick = (min(after_wake, key=_tr_local) if after_wake
                    else min(tr_cands, key=_tr_local))
            snap["tr"] = int(pick["score"])
        # HRV — из сырья hrv_data.hrvSummary.lastNightAvg (есть у всех Garmin-юзеров).
        # Кэш garmin_recovery_cache в снимке НЕ используем — всё из сырья (слой 1).
        hrv_sum = (g.get("hrv_data") or {}).get("hrvSummary") or {}
        if hrv_sum.get("lastNightAvg") is not None:
            snap["hrv"] = float(hrv_sum["lastNightAvg"])

    # ── Whoop (HRV/RHR/сон/wake — если ещё не заполнены) ──
    if get_token(db_user_id, "whoop"):
        w = _raw("whoop") or {}
        rec = (w.get("recovery") or {}).get("records") or []
        if rec:
            sc = rec[0].get("score") or {}
            if snap["hrv"] is None and sc.get("hrv_rmssd_milli") is not None:
                snap["hrv"] = round(float(sc["hrv_rmssd_milli"]), 1)
            if snap["rhr"] is None and sc.get("resting_heart_rate") is not None:
                snap["rhr"] = int(round(float(sc["resting_heart_rate"])))
        slp = (w.get("sleep") or {}).get("records") or []
        if slp:
            s0 = slp[0]
            if snap["wake_at"] is None and s0.get("end"):
                snap["wake_at"] = str(s0["end"])  # UTC, с Z
            stage = (s0.get("score") or {}).get("stage_summary") or {}
            total_ms = ((stage.get("total_light_sleep_time_milli") or 0) +
                        (stage.get("total_slow_wave_sleep_time_milli") or 0) +
                        (stage.get("total_rem_sleep_time_milli") or 0))
            if snap["sleep_h"] is None and total_ms:
                snap["sleep_h"] = round(total_ms / 3_600_000, 2)

    # ── Polar ──
    if get_token(db_user_id, "polar"):
        p = _raw("polar") or {}
        nr = p.get("nightly_recharge")
        items = nr if isinstance(nr, list) else ((nr or {}).get("recharges") or (nr or {}).get("items") or [])
        items = [x for x in items if isinstance(x, dict)]
        if items:
            last = items[-1]
            if snap["hrv"] is None and last.get("heart_rate_variability_avg") is not None:
                snap["hrv"] = round(float(last["heart_rate_variability_avg"]), 1)
            if snap["rhr"] is None and last.get("heart_rate_avg") is not None:
                snap["rhr"] = int(float(last["heart_rate_avg"]))
        sl = p.get("sleep")
        nights = sl if isinstance(sl, list) else ((sl or {}).get("nights") or (sl or {}).get("items") or [])
        nights = [x for x in nights if isinstance(x, dict)]
        if nights:
            n = nights[-1]
            if snap["wake_at"] is None and n.get("sleep_end_time"):
                snap["wake_at"] = str(n["sleep_end_time"])

    # ── COROS (суточное recoveryPct + HRV/RHR; времени пробуждения нет) ──
    if get_token(db_user_id, "coros"):
        info = (((_raw("coros") or {}).get("dashboard") or {}).get("data") or {}).get("summaryInfo") or {}
        # Суточное восстановление — то же поле, что берёт нормализатор (recoveryPct)
        rpc = info.get("recoveryPct")
        if snap["bb"] is None and rpc is not None:
            snap["bb"] = max(0, min(100, int(rpc)))
        hrv = (info.get("sleepHrvData") or {}).get("avgSleepHrv")
        if snap["hrv"] is None and hrv and float(hrv) > 0:
            snap["hrv"] = float(hrv)
        rhr = info.get("rhr")
        if snap["rhr"] is None and rhr and int(rhr) > 0:
            snap["rhr"] = int(rhr)

    # Расчётный TR (coros-calc/strava-calc) из unified — для не-Garmin юзеров.
    # Требует, чтобы нормализация прошла ДО сборки снимка (см. scheduled_wakeup_poll).
    if snap["tr"] is None and not get_token(db_user_id, "garmin"):
        from recovery import _unified_calc_tr
        _tr = _unified_calc_tr(db_user_id)
        if _tr:
            snap["tr"] = int(_tr["score"])

    return snap


def _normalize_after_catch(db_user_id: int) -> None:
    """После поимки ночи — перенормализация unified_cache на свежесинканутом сырье.
    Чтобы s3_* (TR/суточное/HRV) стали актуальными сразу, не ждали 06:45.
    Снимок (morning_*) не затирается — save_unified_data обновляет только unified_json/sources/updated_at.
    """
    try:
        from data_normalizer import run_normalization
        run_normalization(db_user_id)
    except Exception as e:
        logger.warning(f"normalize after catch error for uid={db_user_id}: {e}")


async def scheduled_wakeup_poll(context: ContextTypes.DEFAULT_TYPE):
    """Опросник пробуждения: 06:00–09:00 МСК каждые 15 мин.
    Для юзеров с ночным сервисом, у кого сегодня ночь ещё не поймана (morning_caught≠сегодня):
    синкает сырьё (слой 1), перепроверяет; если ночь закрыта — ставит morning_caught и
    исключает до завтра. Только забор данных — нормализация/снимок не трогаются.
    """
    from database import get_morning_caught, set_morning_caught
    from datetime import datetime, timezone, timedelta
    MSK = timezone(timedelta(hours=3))
    now_msk = datetime.now(MSK)
    # Окно работы: 06:00–09:00 МСК. Вне окна — выход.
    if not (6 <= now_msk.hour < 9):
        return
    today = now_msk.strftime("%Y-%m-%d")

    users = get_all_users()
    caught_now = synced = 0
    for telegram_id, name, _ in users:
        db_user_id = get_or_create_user(telegram_id, name)
        if not _night_services(db_user_id):
            continue
        # уже поймали сегодня — пропускаем
        flag = get_morning_caught(db_user_id)
        caught_today = flag and flag.get("caught") and flag.get("date") == today
        # пойман сегодня И (TR заполнен ИЛИ не Garmin) — пропускаем
        if caught_today and (flag.get("tr") is not None or not get_token(db_user_id, "garmin")):
            continue
        # ночь уже готова по текущему сырью?
        if not caught_today and _night_ready(db_user_id, today):
            _normalize_after_catch(db_user_id)
            set_morning_caught(db_user_id, today, snapshot=_collect_morning_snapshot(db_user_id))
            caught_now += 1
            continue
        # не готова — синкаем свежее сырьё и перепроверяем
        try:
            await _sync_night_services(db_user_id)
            synced += 1
        except Exception as e:
            logger.warning(f"wakeup_poll sync error for {telegram_id}: {e}")
        if _night_ready(db_user_id, today):
            _normalize_after_catch(db_user_id)
            set_morning_caught(db_user_id, today, snapshot=_collect_morning_snapshot(db_user_id))
            caught_now += 1
        await asyncio.sleep(1)

    logger.info(f"Опросник пробуждения: поймано сегодня={caught_now}, синков={synced} (дата {today})")


# ── ПЛАНИРОВЩИК ──────────────────────────────────────────────

def _save_workout_templates(analysis: dict | None, live: dict | None) -> None:
    """Сохраняет эталоны (готовый Garmin JSON) по всем группам из анализа Шага 1.
    Только интервальные. Не критично — сбой не должен ронять рассылку/отчёт.
    """
    import json as _json
    from fit_generator import build_garmin_from_analysis
    if not analysis:
        return
    tmpl_date = live.get("workout_date") if live else None
    if not tmpl_date:
        return
    try:
        parsed = _json.loads(analysis.get("analyzed_json") or "{}")
    except Exception as e:
        logger.error(f"эталоны: не распарсился analyzed_json: {e}")
        return
    saved = 0
    for g in (parsed.get("groups") or []):
        gnum = str(g.get("number") or "").strip()
        if not gnum:
            continue
        try:
            wj = build_garmin_from_analysis(parsed, gnum)
            save_workout_template(tmpl_date, gnum, "interval",
                                  _json.dumps(wj, ensure_ascii=False))
            saved += 1
        except Exception as e:
            logger.error(f"эталон группы {gnum} не сохранён: {e}")
    logger.info(f"эталоны тренировки: {saved} групп на {tmpl_date}")


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
    analysis, status = get_latest_workout_analysis(
        wtype, cur_post, live.get("workout_date") if live else None, cur_edit)
    if status == "empty":
        logger.info(f"Вечерняя рассылка: нет анализа в кэше ({wtype}) — пропуск")
        return

    users = get_all_users_with_status()
    count = 0
    for telegram_id, name, _un, _has in users:
        try:
            await _send_recommendation(telegram_id, name, context, long=is_long, live=live, is_broadcast=True)
            count += 1
            await asyncio.sleep(0.5)
        except Forbidden:
            _mark_user_inactive(telegram_id)
            logger.info(f"Пользователь {telegram_id} заблокировал бота (вечерняя рассылка)")
        except Exception as e:
            logger.error(f"Evening notification error for {telegram_id}: {e}")
    logger.info(f"Вечерняя рассылка завершена ({wtype}, status={status}): {count} отправлено (кэш, без парсинга на лету)")

    # Эталоны по группам в БД (только интервалы) — для последующей сверки факт/план.
    if not is_long:
        _save_workout_templates(analysis, live)

    base = (f"📨 Рассылка завершена\n"
            f"Тип: {wtype} | Отправлено: {count} пользователям")

    # Отчёт по разосланным рекомендациям — НЕ критичен, не должен ронять уведомление.
    report = ""
    try:
        bcast_date = live.get("workout_date") if live else None
        recs = get_recommendations_for_date(bcast_date) if bcast_date else []
        if recs:
            groups: dict[str, list] = {}
            for r in recs:
                groups.setdefault(str(r["recommended_group"] or "—"), []).append(r)
            summary = " · ".join(f"гр{g}: {len(lst)}" for g, lst in groups.items())
            lines = [f"📊 Группы: {summary}"]
            if BROADCAST_REPORT_DETAILED:
                for g, lst in groups.items():
                    lines.append(f"\n▸ Группа {g}")
                    for r in lst:
                        rs = r["evening_recovery_score"]
                        mark = "↓" if r["lowered_by_recovery"] else ""
                        nick = f" (@{r['username']})" if r.get("username") else ""
                        lines.append(f"   {r['name']}{nick} (rec={rs if rs is not None else '—'}{mark})")
            report = "\n".join(lines)
    except Exception as e:
        logger.error(f"Отчёт по рассылке не собрался: {e}")
        report = ""

    # Отправка: влезает в лимит — одним сообщением; иначе базовое + отчёт отдельно (с разбивкой).
    try:
        if report and len(base) + 2 + len(report) <= 4000:
            await _notify_admin(context.bot, f"{base}\n\n{report}")
        else:
            await _notify_admin(context.bot, base)
            if report:
                chunk: list[str] = []
                clen = 0
                for ln in report.split("\n"):
                    if chunk and clen + len(ln) + 1 > 4000:
                        await _notify_admin(context.bot, "\n".join(chunk))
                        chunk, clen = [], 0
                    chunk.append(ln)
                    clen += len(ln) + 1
                if chunk:
                    await _notify_admin(context.bot, "\n".join(chunk))
    except Exception as e:
        logger.error(f"Отправка отчёта по рассылке: {e}")


async def scheduled_cache_refresh(context: ContextTypes.DEFAULT_TYPE):
    """03:45 UTC (06:45 МСК) — обновляет кэш всех сервисов перед утренней рассылкой.
    Порядок: до scheduled_morning (04:00 UTC / 07:00 МСК).
    Обновляет: Strava CTL/ATL/TSB, Garmin recovery+VO2max, COROS, Polar.
    """
    logger.info("Запускаю обновление кэша всех сервисов (03:45 UTC)...")
    users = get_all_users()
    counts = {"strava": 0, "garmin": 0, "coros": 0, "polar": 0, "whoop": 0, "vo2max": 0, "normalized": 0}

    for telegram_id, name, _ in users:
        db_user_id = get_or_create_user(telegram_id, name)

        # ── Strava CTL/ATL/TSB ────────────────────────────────
        try:
            access_token = await ensure_valid_token(db_user_id)
            if access_token:
                await refresh_athlete_cache(db_user_id, access_token)
                from strava import fetch_raw as _strava_fetch_raw
                await _strava_fetch_raw(db_user_id)
                counts["strava"] += 1
        except Exception as e:
            logger.warning(f"Strava cache error for {telegram_id}: {e}")

        # Ночной забор сырья (Garmin/COROS/Polar/Whoop) + нормализация —
        # перенесены в scheduled_wakeup_poll (по факту поимки ночи). Здесь не дублируем.

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

        # Слой 2: нормализация raw_service_data → unified_cache
        try:
            from data_normalizer import run_normalization
            if run_normalization(db_user_id):
                counts["normalized"] += 1
        except Exception as e:
            logger.warning(f"Normalization error for {telegram_id}: {e}")

        await asyncio.sleep(1)

    logger.info(
        f"Кэш обновлён: Strava={counts['strava']}, Garmin={counts['garmin']}, "
        f"COROS={counts['coros']}, Polar={counts['polar']}, Whoop={counts['whoop']}, "
        f"VO2max изменён={counts['vo2max']}, normalized={counts['normalized']}"
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
    # вт/пт — 07:00 МСК. Воскресенье вынесено в scheduled_morning_sunday (07:30 МСК).
    if now.weekday() not in [1, 4]:
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


async def scheduled_cache_refresh_sunday(context: ContextTypes.DEFAULT_TYPE):
    """04:15 UTC (07:15 МСК), только вс — позже будничного рефреша,
    чтобы к 07:15 Garmin успел обработать ночной сон. То же тело, что scheduled_cache_refresh."""
    await scheduled_cache_refresh(context)


async def scheduled_morning_sunday(context: ContextTypes.DEFAULT_TYPE):
    """04:30 UTC (07:30 МСК), только вс — после воскресного рефреша."""
    logger.info("Запускаю утреннюю рассылку (вс, 07:30 МСК)...")
    users = [(tid, name, un) for tid, name, un, has in get_all_users_with_status() if has]
    for telegram_id, name, _ in users:
        try:
            await _send_morning_check(telegram_id, context)
            await asyncio.sleep(0.5)
        except Forbidden:
            _mark_user_inactive(telegram_id)
            logger.info(f"Пользователь {telegram_id} заблокировал бота (воскресная рассылка)")
        except Exception as e:
            logger.error(f"Sunday morning notification error for {telegram_id}: {e}")


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

# ── /b — вариант B для себя / /b_user — с выбором юзера (admin only) ──

async def b_self_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вариант B для самого админа — без выбора пользователя."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return

    admin_tid = update.effective_user.id
    db_user_id = get_or_create_user(admin_tid, update.effective_user.full_name or "admin")

    live = await find_next_workout()
    cur_post = live.get("post_id") if live else None
    cur_date = live.get("workout_date") if live else None
    cur_edit = live.get("edit_date") if live else None
    row, status = get_latest_workout_analysis("interval", cur_post, cur_date, cur_edit)

    if status == "empty" or row is None:
        await update.message.reply_text("😔 Нет анонса интервальной тренировки.")
        return

    import json as _json
    try:
        analysis = _json.loads(row.get("analyzed_json") or "{}")
    except Exception:
        analysis = {}

    user_data = {
        "db_user_id": db_user_id,
        "specialization": (get_user_profile(db_user_id) or {}).get("specialization"),
        "recovery": await _get_unified_recovery(db_user_id),
    }

    workout_dict = dict(live) if live else {"workout_date": analysis.get("workout_date", "")}
    workout_dict["workout_type"] = "interval"
    workout_dict["is_past"] = (status == "past")
    workout_dict["even_pace_available"] = analysis.get("even_pace_available")
    weather = await get_weather_for_workout(
        workout_dict.get("location", ""),
        workout_dict.get("workout_date", ""),
        workout_dict.get("schedule", ""),
    )
    weather_line = format_weather_for_message(weather) if weather else ""

    await update.message.reply_text("🧪 Вариант B — запускаю...")
    asyncio.create_task(_send_ai_variant_b(
        admin_tid, analysis, user_data, context,
        workout_dict=workout_dict, weather_line=weather_line,
    ))


async def b_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    users = get_users_list_for_b()
    if not users:
        await update.message.reply_text("Нет пользователей в базе.")
        return
    keyboard = [
        [InlineKeyboardButton(
            u["name"] + (f" (@{u['username']})" if u.get("username") else ""),
            callback_data=f"b_user_{u['db_user_id']}"
        )]
        for u in users
    ]
    await update.message.reply_text(
        "🧪 Вариант B — выбери пользователя:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def b_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id not in ADMIN_TELEGRAM_IDS:
        await query.answer("Нет доступа.")
        return
    await query.answer()

    db_user_id = int(query.data.rsplit("_", 1)[-1])
    admin_tid = query.from_user.id

    live = await find_next_workout()
    cur_post = live.get("post_id") if live else None
    cur_date = live.get("workout_date") if live else None
    cur_edit = live.get("edit_date") if live else None
    row, status = get_latest_workout_analysis("interval", cur_post, cur_date, cur_edit)

    if status == "empty" or row is None:
        await query.edit_message_text("😔 Нет анонса интервальной тренировки.")
        return

    import json as _json
    try:
        analysis = _json.loads(row.get("analyzed_json") or "{}")
    except Exception:
        analysis = {}

    profile = get_user_profile(db_user_id) or {}
    user_name = profile.get("name") or profile.get("username") or f"user_{db_user_id}"
    user_data = {
        "db_user_id": db_user_id,
        "specialization": profile.get("specialization"),
        "recovery": await _get_unified_recovery(db_user_id),
    }

    workout_dict = dict(live) if live else {"workout_date": analysis.get("workout_date", "")}
    workout_dict["workout_type"] = "interval"
    workout_dict["is_past"] = (status == "past")
    workout_dict["even_pace_available"] = analysis.get("even_pace_available")
    weather = await get_weather_for_workout(
        workout_dict.get("location", ""),
        workout_dict.get("workout_date", ""),
        workout_dict.get("schedule", ""),
    )
    weather_line = format_weather_for_message(weather) if weather else ""

    await query.edit_message_text(
        f"🧪 Вариант B — <b>{user_name}</b>\nЗапускаю...",
        parse_mode="HTML",
    )
    asyncio.create_task(_send_ai_variant_b(
        admin_tid, analysis, user_data, context,
        workout_dict=workout_dict, weather_line=weather_line,
    ))


# ── /a_user — вариант A для выбранного пользователя (admin only) ─────

async def a_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    users = get_users_list_for_b()
    if not users:
        await update.message.reply_text("Нет пользователей в базе.")
        return
    keyboard = [
        [InlineKeyboardButton(
            u["name"] + (f" (@{u['username']})" if u.get("username") else ""),
            callback_data=f"a_user_{u['db_user_id']}"
        )]
        for u in users
    ]
    await update.message.reply_text(
        "📊 Вариант A — выбери пользователя:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def a_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id not in ADMIN_TELEGRAM_IDS:
        await query.answer("Нет доступа.")
        return
    await query.answer()

    db_user_id = int(query.data.rsplit("_", 1)[-1])
    users = get_users_list_for_b()
    user = next((u for u in users if u["db_user_id"] == db_user_id), None)
    if not user:
        await query.edit_message_text("Пользователь не найден.")
        return

    msg = await query.edit_message_text(
        f"📊 Вариант A — <b>{user['name']}</b>\nЗапускаю...",
        parse_mode="HTML",
    )
    # _send_recommendation отредактирует msg — результат в чате админа,
    # данные — выбранного пользователя
    await _send_recommendation(user["telegram_id"], user["name"], context, long=False, msg=msg)


# ── /w_user — реальный путь пользователя (admin only) ───────────────────

async def w_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    users = get_users_list_for_b()
    if not users:
        await update.message.reply_text("Нет пользователей в базе.")
        return
    keyboard = [
        [InlineKeyboardButton(
            u["name"] + (f" (@{u['username']})" if u.get("username") else ""),
            callback_data=f"w_user_{u['db_user_id']}"
        )]
        for u in users
    ]
    await update.message.reply_text(
        "🔍 Реальный путь — выбери пользователя:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def w_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id not in ADMIN_TELEGRAM_IDS:
        await query.answer("Нет доступа.")
        return
    await query.answer()

    db_user_id = int(query.data.rsplit("_", 1)[-1])
    users = get_users_list_for_b()
    user = next((u for u in users if u["db_user_id"] == db_user_id), None)
    if not user:
        await query.edit_message_text("Пользователь не найден.")
        return

    prefs = get_preferences(db_user_id) or {}
    user_mode = prefs.get("ai_mode", "smart")
    route = "B (ИИ)" if user_mode != "calc" else "A (формулы)"
    msg = await query.edit_message_text(
        f"🔍 Реальный путь — <b>{user['name']}</b>\nРежим: {user_mode} → {route}\nЗапускаю...",
        parse_mode="HTML",
    )
    # _send_recommendation читает ai_mode выбранного пользователя из БД →
    # deep/smart/fast → B, calc → A. Результат в чате админа через msg.
    await _send_recommendation(user["telegram_id"], user["name"], context, long=False, msg=msg)


# ── /l_user — реальный путь ЛОНГА выбранного пользователя (admin only) ──

async def l_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    users = get_users_list_for_b()
    if not users:
        await update.message.reply_text("Нет пользователей в базе.")
        return
    keyboard = [
        [InlineKeyboardButton(
            u["name"] + (f" (@{u['username']})" if u.get("username") else ""),
            callback_data=f"l_user_{u['db_user_id']}"
        )]
        for u in users
    ]
    await update.message.reply_text(
        "🕐 Лонг — выбери пользователя:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def l_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id not in ADMIN_TELEGRAM_IDS:
        await query.answer("Нет доступа.")
        return
    await query.answer()

    db_user_id = int(query.data.rsplit("_", 1)[-1])
    users = get_users_list_for_b()
    user = next((u for u in users if u["db_user_id"] == db_user_id), None)
    if not user:
        await query.edit_message_text("Пользователь не найден.")
        return

    msg = await query.edit_message_text(
        f"🕐 Лонг — <b>{user['name']}</b>\nЗапускаю...",
        parse_mode="HTML",
    )
    # long=True → _send_recommendation идёт формульным путём (recommend_long).
    # Результат в чате админа через msg.
    await _send_recommendation(user["telegram_id"], user["name"], context, long=True, msg=msg)


async def _send_prompt_text(send_fn, prompt: str) -> None:
    """Отправляет текст промпта кусками по 4096 символов."""
    chunk_size = 4096
    for i in range(0, max(len(prompt), 1), chunk_size):
        await send_fn(prompt[i:i + chunk_size])


async def _build_analysis_and_user_data(db_user_id: int):
    """Возвращает (analysis, user_data, workout_dict, weather_line) или (None, ...) при ошибке."""
    import json as _json
    live = await find_next_workout()
    cur_post = live.get("post_id") if live else None
    cur_date = live.get("workout_date") if live else None
    cur_edit = live.get("edit_date") if live else None
    row, status = get_latest_workout_analysis("interval", cur_post, cur_date, cur_edit)
    if status == "empty" or row is None:
        return None, None, None, None
    try:
        analysis = _json.loads(row.get("analyzed_json") or "{}")
    except Exception:
        analysis = {}
    profile = get_user_profile(db_user_id) or {}
    user_data = {
        "db_user_id": db_user_id,
        "specialization": profile.get("specialization"),
        "recovery": await _get_unified_recovery(db_user_id),
    }
    workout_dict = dict(live) if live else {"workout_date": analysis.get("workout_date", "")}
    workout_dict["workout_type"] = "interval"
    workout_dict["is_past"] = (status == "past")
    workout_dict["even_pace_available"] = analysis.get("even_pace_available")
    weather = await get_weather_for_workout(
        workout_dict.get("location", ""),
        workout_dict.get("workout_date", ""),
        workout_dict.get("schedule", ""),
    )
    weather_line = format_weather_for_message(weather) if weather else ""
    return analysis, user_data, workout_dict, weather_line


# ── /p_b — промпт варианта B для себя (admin only) ──────────────────────

async def p_b_self_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    admin_tid = update.effective_user.id
    db_user_id = get_or_create_user(admin_tid, update.effective_user.full_name or "admin")
    analysis, user_data, workout_dict, _ = await _build_analysis_and_user_data(db_user_id)
    if analysis is None:
        await update.message.reply_text("😔 Нет анонса интервальной тренировки.")
        return
    prompt, _ = await _build_variant_b_prompt(db_user_id, analysis, user_data, workout_dict)
    await _send_prompt_text(update.message.reply_text, prompt)


async def p_b_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    users = get_users_list_for_b()
    if not users:
        await update.message.reply_text("Нет пользователей в базе.")
        return
    keyboard = [
        [InlineKeyboardButton(
            u["name"] + (f" (@{u['username']})" if u.get("username") else ""),
            callback_data=f"pb_user_{u['db_user_id']}"
        )]
        for u in users
    ]
    await update.message.reply_text(
        "📋 Промпт B — выбери пользователя:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def pb_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id not in ADMIN_TELEGRAM_IDS:
        await query.answer("Нет доступа.")
        return
    await query.answer()
    db_user_id = int(query.data.rsplit("_", 1)[-1])
    profile = get_user_profile(db_user_id) or {}
    user_name = profile.get("name") or profile.get("username") or f"user_{db_user_id}"
    analysis, user_data, workout_dict, _ = await _build_analysis_and_user_data(db_user_id)
    if analysis is None:
        await query.edit_message_text("😔 Нет анонса интервальной тренировки.")
        return
    await query.edit_message_text(f"📋 Промпт B — <b>{user_name}</b>", parse_mode="HTML")
    prompt, _ = await _build_variant_b_prompt(db_user_id, analysis, user_data, workout_dict)
    admin_tid = query.from_user.id
    await _send_prompt_text(
        lambda t: context.bot.send_message(admin_tid, t),
        prompt,
    )


# ── /p_a — промпт варианта A для себя (admin only) ──────────────────────

async def _build_step2_facts(db_user_id: int, name: str, long: bool = False):
    """Собирает facts-dict для _build_step2_prompt, аналогично _send_recommendation."""
    import json as _json
    wtype = "long" if long else "interval"
    live = await (find_next_long_run() if long else find_next_workout())
    cur_post = live.get("post_id") if live else None
    cur_date = live.get("workout_date") if live else None
    cur_edit = live.get("edit_date") if live else None
    row, status = get_latest_workout_analysis(wtype, cur_post, cur_date, cur_edit)
    if status == "empty" or row is None:
        return None, None, None
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
        return None, None, (rec or {}).get("note", "Не удалось собрать рекомендацию.")
    if long:
        advice = claude_advisor.recommendation_to_long_advice(rec, analysis, user_data["recovery"])
    else:
        advice = claude_advisor.recommendation_to_advice(rec, analysis, user_data["recovery"])
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
    return facts, rec_mode, None


async def p_a_self_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    admin_tid = update.effective_user.id
    name = update.effective_user.full_name or "admin"
    db_user_id = get_or_create_user(admin_tid, name)
    facts, rec_mode, err = await _build_step2_facts(db_user_id, name, long=False)
    if facts is None:
        await update.message.reply_text(err or "😔 Нет анонса интервальной тренировки.")
        return
    prompt = claude_advisor._build_step2_prompt(facts, rec_mode, False)
    await _send_prompt_text(update.message.reply_text, prompt)


async def p_a_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    users = get_users_list_for_b()
    if not users:
        await update.message.reply_text("Нет пользователей в базе.")
        return
    keyboard = [
        [InlineKeyboardButton(
            u["name"] + (f" (@{u['username']})" if u.get("username") else ""),
            callback_data=f"pa_user_{u['db_user_id']}"
        )]
        for u in users
    ]
    await update.message.reply_text(
        "📋 Промпт A — выбери пользователя:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def pa_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id not in ADMIN_TELEGRAM_IDS:
        await query.answer("Нет доступа.")
        return
    await query.answer()
    db_user_id = int(query.data.rsplit("_", 1)[-1])
    profile = get_user_profile(db_user_id) or {}
    user_name = profile.get("name") or profile.get("username") or f"user_{db_user_id}"
    telegram_id = profile.get("telegram_id") or db_user_id
    facts, rec_mode, err = await _build_step2_facts(db_user_id, user_name, long=False)
    if facts is None:
        await query.edit_message_text(err or "😔 Нет анонса интервальной тренировки.")
        return
    await query.edit_message_text(f"📋 Промпт A — <b>{user_name}</b>", parse_mode="HTML")
    prompt = claude_advisor._build_step2_prompt(facts, rec_mode, False)
    admin_tid = query.from_user.id
    await _send_prompt_text(
        lambda t: context.bot.send_message(admin_tid, t),
        prompt,
    )


# ── /p_analyze — промпт Шага 1 (анализ анонса, admin only) ─────────────

async def p_analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    await update.message.reply_text(
        "📋 Промпт Шага 1 — выбери тип:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚡ Интервальная", callback_data="panalyze_interval"),
            InlineKeyboardButton("🕐 Long Run",     callback_data="panalyze_long"),
        ]])
    )


async def panalyze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id not in ADMIN_TELEGRAM_IDS:
        await query.answer("Нет доступа.")
        return
    await query.answer()

    wtype = "long" if query.data == "panalyze_long" else "interval"
    row, status = get_latest_workout_analysis(wtype)
    if status == "empty" or row is None:
        await query.edit_message_text(f"😔 Нет анализа {wtype} в базе.")
        return

    raw_text = row.get("raw_text") or ""
    if not raw_text:
        await query.edit_message_text("😔 raw_text не сохранён в базе.")
        return

    # comments_text не хранится в БД — пробуем live с проверкой post_id
    comments_text = "(нет комментариев)"
    try:
        live = await (find_next_long_run() if wtype == "long" else find_next_workout())
        if live and live.get("post_id") == row.get("post_id"):
            comments_text = live.get("comments_text") or "(нет комментариев)"
    except Exception:
        pass

    prompt = claude_advisor._build_analyze_prompt(raw_text, comments_text)
    await query.edit_message_text(f"📋 Промпт Шага 1 — {wtype}")
    admin_tid = query.from_user.id
    await _send_prompt_text(
        lambda t: context.bot.send_message(admin_tid, t),
        prompt,
    )


# ── АКТИВНОСТЬ ПОЛЬЗОВАТЕЛЕЙ ──────────────────────────────────────────

# Основные inline-кнопки логируем КАК команды — /stats считает их вместе
_BTN_TO_CMD = {
    "get_workout":  "/workout",
    "get_long_run": "/long",
    "get_morning":  "/morning",
}


async def _activity_logger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сквозной логгер действий (group=-1, не блокирует остальные хендлеры).
    Пишет в user_activity только ВХОДЯЩИЕ действия юзера: команды (/x) и
    inline-кнопки (btn:<data>; главные кнопки маппятся в /workout|/long|/morning).
    Рассылки — исходящие, сюда не попадают по построению."""
    try:
        u = update.effective_user
        if not u:
            return
        action = None
        if update.callback_query and update.callback_query.data:
            data = update.callback_query.data
            action = _BTN_TO_CMD.get(data) or ("btn:" + data[:40])
        elif update.message and update.message.text and update.message.text.startswith("/"):
            action = update.message.text.split()[0].split("@")[0][:40]
        if action:
            db_user_id = get_or_create_user(u.id, u.full_name or "", u.username)
            log_activity(db_user_id, action)
    except Exception:
        pass  # логгер никогда не должен мешать основной логике


async def cmd_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/activity (admin) — активность по дням + топ действий за 14 дней."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    days = 14
    daily = get_activity_daily(days)
    top = get_activity_top(days)
    lines = [f"📈 Активность за {days} дней (дни МСК, без рассылок)", ""]
    if daily:
        for d, users_cnt, actions_cnt in daily:
            lines.append(f"{d}: {users_cnt} чел · {actions_cnt} действий")
    else:
        lines.append("Нет данных.")
    if top:
        lines += ["", "Топ действий:"]
        for cmd, cnt, uniq in top:
            lines.append(f"  {cmd}: {cnt} (юзеров: {uniq})")
    who = get_activity_users(days)
    if who:
        lines += ["", f"Кто активен ({len(who)}):"]
        for name, username, cnt, last_d in who:
            nick = f" (@{username})" if username else ""
            lines.append(f"  {name}{nick}: {cnt} действий, посл. {last_d}")
    lines += ["", "Кнопки Тренировка/Long Run/Утро считаются как /workout, /long, /morning."]
    await update.message.reply_text("\n".join(lines))


async def cmd_msg_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/msg_user <id> <текст> (admin) — отправить сообщение юзеру от имени бота.
    id — внутренний db_user_id (как в отчётах/слепках). Текст — всё после id,
    переносы строк сохраняются. Отправляется без parse_mode (как есть)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    args = context.args or []
    from database import get_connection
    # Без аргументов — список юзеров кнопками (как /w_user)
    if not args:
        users = get_users_list_for_b()
        if not users:
            await update.message.reply_text("Нет пользователей.")
            return
        keyboard = [
            [InlineKeyboardButton(
                u["name"] + (f" (@{u['username']})" if u.get("username") else ""),
                callback_data=f"msgu_{u['db_user_id']}"
            )]
            for u in users
        ]
        await update.message.reply_text(
            "✉️ Кому написать от имени бота?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return
    raw_id = args[0]
    with get_connection() as conn:
        if raw_id.startswith("@"):
            row = conn.execute(
                "SELECT telegram_id, name, id FROM users WHERE username=? COLLATE NOCASE",
                (raw_id[1:],)).fetchone()
        else:
            try:
                target_uid = int(raw_id)
            except ValueError:
                await update.message.reply_text(
                    "Формат: /msg_user [id или @username] [текст]. Без аргументов — список кнопками.")
                return
            row = conn.execute("SELECT telegram_id, name, id FROM users WHERE id=?",
                               (target_uid,)).fetchone()
    if not row:
        await update.message.reply_text(f"Юзер {raw_id} не найден.")
        return
    tg_id, name, db_uid = row[0], row[1], row[2]
    # Адресат без текста — ждём текст следующим сообщением
    if len(args) < 2:
        context.user_data["awaiting_msg_user"] = {"uid": db_uid, "tg_id": tg_id, "name": name}
        await update.message.reply_text(
            f"✉️ Напиши текст для {name} следующим сообщением (или напиши «отмена»).")
        return
    # Адресат + текст одной командой (split сохраняет переносы внутри текста)
    text = update.message.text.split(maxsplit=2)[2]
    try:
        await context.bot.send_message(tg_id, text)
        await update.message.reply_text(f"✅ Отправлено: {name} ({raw_id})")
    except Exception as e:
        await update.message.reply_text(
            f"❌ Не отправилось для {raw_id}: {type(e).__name__}: {e}")


async def msg_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатие на юзера в списке /msg_user — ждём текст следующим сообщением."""
    query = update.callback_query
    if query.from_user.id not in ADMIN_TELEGRAM_IDS:
        await query.answer("Нет доступа.")
        return
    await query.answer()
    db_uid = int(query.data.rsplit("_", 1)[-1])
    users = get_users_list_for_b()
    user = next((u for u in users if u["db_user_id"] == db_uid), None)
    if not user:
        await query.edit_message_text("Пользователь не найден.")
        return
    context.user_data["awaiting_msg_user"] = {
        "uid": db_uid, "tg_id": user["telegram_id"], "name": user["name"]}
    await query.edit_message_text(
        f"✉️ Напиши текст для {user['name']} следующим сообщением (или напиши «отмена»).")


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/last (admin) — разбор последней выполненной DD-тренировки: графики факт vs план.
    Не затрагивает рабочие ветки. Источник плана — Garmin workout по workoutId."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    db_user_id = get_or_create_user(update.effective_user.id, update.effective_user.full_name)
    # Аргумент: /last dark (или d/тёмная) → тёмная тема; без аргумента — светлая
    arg = (context.args[0].lower() if context.args else "")
    dark = arg in ("dark", "d", "тёмная", "темная", "black", "night", "ночь")
    msg = await update.message.reply_text("⏳ Собираю разбор последней тренировки…")
    try:
        from activity_review import build_review
        res = await build_review(db_user_id, dark=dark)
    except Exception as e:
        logger.error(f"/last error for {update.effective_user.id}: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка разбора: {type(e).__name__}: {e}")
        return
    if not res.get("ok"):
        await msg.edit_text(f"⚠️ {res.get('msg')}")
        return
    await msg.edit_text(
        f"📊 Разбор: {res['name']}\n"
        f"рабочих отрезков: {res['n_work']}, отдыха: {res['n_rest']}")
    items = [(res.get("work_png"), "Рабочие интервалы"),
             (res.get("rest_png"), "Отдых"),
             (res.get("table_png"), "Таблица повторов")]
    items = [(p, c) for p, c in items if p]
    for i, (png, cap) in enumerate(items):
        # к последней картинке прикрепляем кнопку «Главное меню»
        markup = _add_main_menu_btn(None) if i == len(items) - 1 else None
        with open(png, "rb") as f:
            await context.bot.send_photo(update.effective_user.id, photo=f,
                                         caption=cap, reply_markup=markup)


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ai (admin) — ИИ-анализ тренировки: собирает пакет данных (профиль, план,
    факт по отрезкам, утренний снимок) и шлёт в DeepSeek, возвращает разбор тренера.
    Read-only, рабочие ветки не трогает.
    /ai — последняя DD; /ai DD_20260612 | /ai 23219097987 — выбор тренировки;
    /ai simple|s [селектор] — только графики, без вызова ИИ;
    /ai data [селектор] — сырой пакет данных + промпт (без вызова ИИ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    db_user_id = get_or_create_user(update.effective_user.id, update.effective_user.full_name)
    args = list(context.args or [])
    raw_mode = bool(args) and args[0].lower() in ("data", "raw", "данные")
    if raw_mode:
        args = args[1:]
    simple_mode = bool(args) and args[0].lower() in ("simple", "s")
    if simple_mode:
        args = args[1:]
    selector = args[0] if args else None

    import html

    def _send_chunks(text, pre=False):
        chunks, chunk, size = [], [], 0
        for line in text.split("\n"):
            if size + len(line) + 1 > 3800 and chunk:
                chunks.append("\n".join(chunk))
                chunk, size = [], 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            chunks.append("\n".join(chunk))
        return chunks

    if raw_mode:
        msg = await update.message.reply_text("⏳ Собираю пакет данных…")
        try:
            from ai_package import build_package, PROMPT
            res = await build_package(db_user_id, selector)
        except Exception as e:
            logger.error(f"/ai data error for {update.effective_user.id}: {e}", exc_info=True)
            await msg.edit_text(f"❌ Ошибка сборки: {type(e).__name__}: {e}")
            return
        if not res.get("ok"):
            await msg.edit_text(f"⚠️ {res.get('msg')}")
            return
        await msg.edit_text(f"📦 Пакет данных: {res['name']}")
        full = PROMPT + "\n\n" + res["text"]
        for ch in _send_chunks(full):
            await context.bot.send_message(
                update.effective_user.id, f"<pre>{html.escape(ch)}</pre>", parse_mode="HTML")
        return

    wait = ("⏳ Собираю данные и графики…" if simple_mode else
            "⏳ Собираю данные, графики и анализ через DeepSeek…\nМожет занять 1-3 мин.")
    msg = await update.message.reply_text(wait)
    try:
        from ai_package import build_package, build_charts, PROMPT
        res = await build_package(db_user_id, selector)
    except Exception as e:
        logger.error(f"/ai error for {update.effective_user.id}: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка сборки: {type(e).__name__}: {e}")
        return
    if not res.get("ok"):
        await msg.edit_text(f"⚠️ {res.get('msg')}")
        return

    # Графики из уже добытых данных (один поход в Garmin внутри build_package).
    await msg.edit_text(f"📊 Тренировка: {res['name']}")
    try:
        charts = await build_charts(res.get("splits"), res.get("plan_steps"),
                                    res["name"], "/tmp", str(db_user_id))
    except Exception as e:
        logger.error(f"/ai charts error: {e}", exc_info=True)
        charts = {}
    for png, cap in [(charts.get("work_png"), "Рабочие интервалы"),
                     (charts.get("rest_png"), "Отдых"),
                     (charts.get("table_png"), "Таблица повторов")]:
        if not png:
            continue
        with open(png, "rb") as f:
            await context.bot.send_photo(update.effective_user.id, photo=f, caption=cap)

    if simple_mode:
        return

    # ИИ-анализ (полный режим): тот же пакет, без повторного похода в Garmin.
    import claude_advisor
    answer = await asyncio.to_thread(
        claude_advisor.ask_text, PROMPT + "\n\n" + res["text"], "deep")
    if not answer:
        await context.bot.send_message(update.effective_user.id, "⚠️ ИИ не ответил.")
        return
    # Страховка: убираем остатки Markdown (отправляем как простой текст).
    import re as _re_md
    _ans = answer
    _ans = _re_md.sub(r"\*\*(.+?)\*\*", r"\1", _ans)   # **жирный** → текст
    _ans = _re_md.sub(r"__(.+?)__", r"\1", _ans)         # __жирный__ → текст
    _clean = []
    for _ln in _ans.split("\n"):
        _s = _ln.lstrip()
        _s = _re_md.sub(r"^#{1,6}\s*", "", _s)            # заголовки #
        _s = _re_md.sub(r"^[\*\-]\s+", "— ", _s)           # маркеры *,- → —
        _clean.append(_s)
    _ans = "\n".join(_clean).strip()
    for ch in _send_chunks(_ans):
        await context.bot.send_message(update.effective_user.id, ch)


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
    app.add_handler(CommandHandler("b",         b_self_command))
    app.add_handler(CommandHandler("b_user",    b_command))
    app.add_handler(CommandHandler("a_user",    a_user_command))
    app.add_handler(CommandHandler("w_user",    w_user_command))
    app.add_handler(CommandHandler("l_user",    l_user_command))
    app.add_handler(CommandHandler("p_b",       p_b_self_command))
    app.add_handler(CommandHandler("p_b_user",  p_b_command))
    app.add_handler(CommandHandler("p_a",       p_a_self_command))
    app.add_handler(CommandHandler("p_a_user",  p_a_command))
    app.add_handler(CommandHandler("p_analyze", p_analyze_command))
    app.add_handler(CommandHandler("activity",  cmd_activity))
    app.add_handler(CommandHandler("msg_user",  cmd_msg_user))
    app.add_handler(CommandHandler("last",      cmd_last))
    app.add_handler(CommandHandler("ai",        cmd_ai))
    app.add_handler(CallbackQueryHandler(msg_user_callback,  pattern=r"^msgu_\d+$"))
    app.add_handler(CallbackQueryHandler(b_user_callback,   pattern=r"^b_user_\d+$"))
    app.add_handler(CallbackQueryHandler(a_user_callback,   pattern=r"^a_user_\d+$"))
    app.add_handler(CallbackQueryHandler(w_user_callback,   pattern=r"^w_user_\d+$"))
    app.add_handler(CallbackQueryHandler(l_user_callback,   pattern=r"^l_user_\d+$"))
    app.add_handler(CallbackQueryHandler(pb_user_callback,  pattern=r"^pb_user_\d+$"))
    app.add_handler(CallbackQueryHandler(pa_user_callback,  pattern=r"^pa_user_\d+$"))
    app.add_handler(CallbackQueryHandler(panalyze_callback, pattern=r"^panalyze_(interval|long)$"))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(global_error_handler)
    # Сквозной логгер активности: group=-1 выполняется НЕЗАВИСИМО от основных хендлеров
    app.add_handler(TypeHandler(Update, _activity_logger), group=-1)

    job_queue = app.job_queue
    job_queue.run_daily(scheduled_evening,       time=time(hour=17, minute=0))                          # 20:00 МСК
    # PTB days: 0=вс, 1=пн … 6=сб → вт/пт = (2, 5), вс = (0,)
    job_queue.run_daily(scheduled_cache_refresh, time=time(hour=2,  minute=0),  days=(2, 5))            # 05:00 МСК вт/пт
    job_queue.run_daily(scheduled_morning,       time=time(hour=4,  minute=0),  days=(2, 5))            # 07:00 МСК вт/пт
    job_queue.run_daily(scheduled_cache_refresh_sunday, time=time(hour=4, minute=15), days=(0,))        # 07:15 МСК вс
    job_queue.run_daily(scheduled_morning_sunday,       time=time(hour=4, minute=30), days=(0,))        # 07:30 МСК вс
    job_queue.run_repeating(scheduled_new_workout_check, interval=1800, first=60)                       # каждые 30 мин
    job_queue.run_repeating(scheduled_wakeup_poll, interval=900, first=120)                              # каждые 15 мин (окно 06:00–09:00 МСК внутри)

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