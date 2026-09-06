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
    get_activity_daily, get_activity_top, get_activity_users, get_activity_report,
    get_report_users,
    delete_token,
    get_all_users_with_details, get_users_with_service_full, get_users_with_profile_full,
    save_feedback, save_rating, get_recent_ratings, get_recent_feedbacks,
    save_workout_analysis, get_workout_analysis, get_latest_workout_analysis,
    save_vo2max_device, save_vo2max_manual, set_vo2max_priority,
    save_lt_device, save_lt_manual, set_lt_priority,
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
        # 17.08.2026 (Антон): Разбор на место Утра, Утро на место Профиля,
        # Профиль в третью строку вместе с новой кнопкой Уведомлений.
        [InlineKeyboardButton("📊 Разбор тренировки", callback_data="get_report"),
         InlineKeyboardButton("☀️ Утро",       callback_data="get_morning")],
        [InlineKeyboardButton("👤 Профиль",   callback_data="my_profile"),
         InlineKeyboardButton("🔔 Уведомления", callback_data="notifications")],
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
    ("coros_mcp", "⌚", "COROS без пароля", "connect_coros_oauth_btn", "COROS отключён"),
    ("polar",  "❄️", "Polar",   "connect_polar_btn",   "Polar отключён"),
]


def _svc_name(svc: str) -> str:
    """Возвращает отображаемое имя сервиса по ключу."""
    return next((name for s, _, name, _, _ in _SERVICES if s == svc), svc)


def _svc_done_msg(svc: str) -> str:
    """Возвращает сообщение после отключения сервиса."""
    return next((msg for s, _, _, _, msg in _SERVICES if s == svc), f"{svc} отключён")


async def _revoke_service(db_user_id: int, svc: str) -> bool:
    """Отзыв доступа НА СТОРОНЕ сервиса перед удалением ключа у нас.

    Strava: освобождает слот в лимите приложения — без этого отключённые
    продолжают занимать места. Polar: снимает регистрацию в AccessLink.
    У остальных сервисов отзыва нет — возвращаем False, ключ всё равно удаляется.
    """
    try:
        if svc == "strava":
            import strava as _sv
            return await _sv.deauthorize(db_user_id)
        if svc == "polar":
            import polar as _pl
            return await _pl.deregister(db_user_id)
    except Exception as e:
        logger.warning(f"Отзыв {svc} для uid={db_user_id} не удался: {e}")
    return False


def _blocked_with_tokens() -> list:
    """[(uid, кто, [сервисы], с какого числа), ...] — заблокировавшие бота,
    у кого остались ключи Strava/Polar (такие зря занимают места в лимите)."""
    from database import get_connection
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT u.id, COALESCE(u.username, u.name, CAST(u.telegram_id AS TEXT)), "
            "p.deactivated_at FROM users u JOIN user_preferences p ON p.user_id = u.id "
            "WHERE p.is_active = 0 ORDER BY u.id").fetchall()
    out = []
    for uid, who, since in rows:
        svcs = [s for s in ("strava", "polar", "garmin", "coros", "whoop") if get_token(uid, s)]
        if svcs:
            out.append((uid, who, svcs, (since or "")[:10]))
    return out


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
            "📢 Автоматически уведомляю когда выходит новый анонс тренировки\n"
            "📊 Разбираю прошедшую тренировку — графики и анализ от ИИ. "
            "Пошаговая инструкция — /howto (и кнопка «📖 Как получить разбор» под рекомендациями)\n\n"
            "🌐 О сервисе и инструкции — dodick.run\n\n"
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

async def admin_video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ шлёт видео боту в личку → file_id сохраняется как видеоинструкция
    (17.08.2026, раздел «🎬 Видеоинструкция» в Справке). Не-админские видео игнорируются."""
    if (update.effective_user.id not in ADMIN_TELEGRAM_IDS
            or not update.message or not update.message.video):
        return
    fid = update.message.video.file_id
    from database import get_connection
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) "
            "VALUES ('video_manual_file_id', ?)", (fid,))
    await update.message.reply_text(
        "🎬 Видео сохранено как инструкция — кнопка в Справке уже отдаёт его.")


async def _report_block(bot, telegram_id: int, where: str) -> None:
    """Блокировка бота пользователем: пометить неактивным + уведомить админа
    (задача 17.08.2026: блокировки жили только в логе и терялись)."""
    _mark_user_inactive(telegram_id)
    logger.info(f"Пользователь {telegram_id} заблокировал бота ({where})")
    try:
        from database import get_connection
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(username, name, CAST(telegram_id AS TEXT)) "
                "FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        who = f"@{row[0]}" if row else str(telegram_id)
        await _notify_admin(bot, f"🚫 Заблокировал бота: {who} ({where})")
    except Exception as e:
        logger.warning(f"_report_block notify failed for {telegram_id}: {e}")


async def check_new_users(context: ContextTypes.DEFAULT_TYPE) -> None:
    """20.08.2026: источник правды — ТАБЛИЦА users. Раз в несколько минут смотрим,
    не появились ли записи новее отметки последней проверки (bot_settings.
    last_new_user_seen) — тогда шлём админу. Ловит любой путь появления юзера
    (не только /start) и не трогает остальные апдейты."""
    from database import get_connection
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT value FROM bot_settings WHERE key = 'last_new_user_seen'").fetchone()
            if not row or not row[0]:
                conn.execute(
                    "INSERT OR REPLACE INTO bot_settings (key, value) "
                    "VALUES ('last_new_user_seen', datetime('now'))")
                return
            fresh = conn.execute(
                "SELECT name, username, created_at FROM users "
                "WHERE created_at > ? ORDER BY created_at", (row[0],)).fetchall()
            if fresh:
                conn.execute(
                    "INSERT OR REPLACE INTO bot_settings (key, value) VALUES "
                    "('last_new_user_seen', ?)", (fresh[-1][2],))
    except Exception as e:
        logger.error(f"check_new_users: {e}")
        return
    for name, uname, _ in fresh:
        tag = f" (@{uname})" if uname else ""
        await _notify_admin(
            context.bot,
            f"👤 Новый пользователь: {name}{tag}\n"
            f"Всего пользователей: {len(get_all_users())}")
    await _check_new_ratings(context)


async def _check_new_ratings(context: ContextTypes.DEFAULT_TYPE) -> None:
    """21.08.2026: новые оценки рекомендаций — тот же приём, что и с новыми юзерами:
    смотрим записи recommendation_ratings новее отметки bot_settings.last_rating_seen.
    Первый запуск только ставит отметку — старые оценки не хлынут."""
    from database import get_connection
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT value FROM bot_settings WHERE key = 'last_rating_seen'").fetchone()
            if not row or not row[0]:
                conn.execute(
                    "INSERT OR REPLACE INTO bot_settings (key, value) "
                    "VALUES ('last_rating_seen', datetime('now'))")
                return
            fresh = conn.execute("""
                SELECT COALESCE(u.username, u.name), r.rating, r.workout_date,
                       r.ai_mode, r.comment, r.created_at
                FROM recommendation_ratings r JOIN users u ON u.id = r.user_id
                WHERE r.created_at > ? ORDER BY r.created_at
            """, (row[0],)).fetchall()
            if fresh:
                conn.execute(
                    "INSERT OR REPLACE INTO bot_settings (key, value) VALUES "
                    "('last_rating_seen', ?)", (fresh[-1][5],))
    except Exception as e:
        logger.error(f"_check_new_ratings: {e}")
        return
    for who, rating, wdate, mode, comment, _ in fresh:
        text = (f"⭐ Оценка {rating}/10 от {who}\n"
                f"Тренировка: {wdate or '—'} · режим: {mode or '—'}")
        if comment:
            text += f"\n💬 {comment[:500]}"
        await _notify_admin(context.bot, text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = not user_exists(user.id)
    was_blocked = False
    if not is_new:
        # Вернулся ли после блокировки (17.08.2026: раньше такие возвраты были невидимы)
        from database import get_connection
        with get_connection() as conn:
            row = conn.execute(
                "SELECT p.is_active FROM users u "
                "LEFT JOIN user_preferences p ON p.user_id = u.id "
                "WHERE u.telegram_id = ?", (user.id,)).fetchone()
        was_blocked = bool(row) and row[0] == 0
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)
    if is_new:
        # 20.08.2026: уведомляет check_new_users (по таблице users) — здесь не дублируем
        pass
    elif was_blocked:
        uname = f" (@{user.username})" if user.username else ""
        await _notify_admin(
            context.bot,
            f"🔓 Вернулся после блокировки: {user.full_name}{uname}"
        )
    await _show_main_menu(update, user, db_user_id)


def _parse_cmd_date(arg: str) -> str | None:
    """Дата из аргумента команды в формат базы ГГГГ-ММ-ДД.

    8 цифр — ГГГГММДД, 4 цифры — ММДД с текущим годом.
    Возвращает None, если разобрать не удалось.
    """
    arg = (arg or "").strip()
    if not arg:
        return None
    if len(arg) == 4:
        arg = f"{datetime.now().year}{arg}"
    try:
        return datetime.strptime(arg, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


async def cmd_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = _mark_user_active_if_needed(user.id, user.full_name, user.username)

    arg = context.args[0] if context.args else ""
    target_date = _parse_cmd_date(arg)
    if arg and not target_date:
        await update.message.reply_text(
            "Не понял дату. Формат: /workout 20260905 или /workout 0905"
        )
        return

    if target_date:
        msg = await update.message.reply_text(f"🔍 Ищу анонс на {arg}...")
    else:
        msg = await update.message.reply_text("🔍 Ищу анонс, анализирую и подбираю группу...")

    await _send_recommendation(user.id, user.full_name, context, long=False, msg=msg,
                               target_date=target_date)


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


GARMIN_HOWTO = (
    "📖 Как получить разбор тренировки (Garmin)\n\n"
    "1. При получении рекомендации загрузи тренировку в часы — нажми кнопку "
    "с нужной группой (можно сразу две, если сложно определиться). "
    "Больше ничего делать не надо — за ночь часы синхронизируются с Garmin "
    "Connect и заберут тренировку. Если этого не произошло — выполни "
    "синхронизацию вручную.\n\n"
    "2. На тренировке выбирай тип «Бег на стадионе», а из библиотеки — "
    "соответствующую тренировку (имя вида DD_YYYYMMDD-<группа>_lvl). "
    "Дальше выбери «Начать тренировку» и дождись сигнала GPS. "
    "Разминку и заминку не включай в тренировку.\n\n"
    "3. За всю тренировку нужно только дважды нажать кнопку: старт в начале "
    "и стоп по окончании. Часы сами отсекают все отрезки — не отсекай их "
    "кнопкой, иначе собьёшь структуру! И будь внимателен: пейсеры иногда "
    "начинают с дальнего виража — нажимай старт с началом первого рабочего "
    "отрезка.\n\n"
    "4. Если проблема с GPS — выбери тип «Беговой тренажёр». Тренировку из "
    "библиотеки не запускай — она будет только эталоном. Круги отсекай "
    "самостоятельно по разметке стадиона, сохраняя структуру тренировки: "
    "бот сопоставит отрезки эталона с фактом — расстояние берётся из задания, "
    "а время — фактическое из часов.\n\n"
    "5. По окончании часы сами отправят файл в Garmin Connect с именем по маске. "
    "В режиме «Беговой тренажёр» переименуй активность вручную по маске.\n\n"
    "6. Дальше в боте нажми кнопку «Разбор тренировки» — придёт таблица "
    "сравнения эталона с фактом, графики и персональный анализ качества "
    "работы с рекомендациями на будущее.\n\n"
    "Итого — 4 клика: отправить эталон в часы → старт → стоп → разбор.\n"
    "Вопросы — в обратную связь."
)


async def cmd_howto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/howto — инструкция «Как получить разбор» (тот же текст, что по кнопке)."""
    _mark_user_active_if_needed(update.effective_user.id,
                                update.effective_user.full_name,
                                update.effective_user.username)
    await update.message.reply_text(GARMIN_HOWTO)


def _pace_feedback_row() -> list:
    """Строка кнопок фидбека по темпу рекомендации (перед оценкой)."""
    return [
        InlineKeyboardButton("🚀 Готов быстрее", callback_data="pfb_faster"),
        InlineKeyboardButton("🎯 В точку", callback_data="pfb_ok"),
        InlineKeyboardButton("🐢 Помедленнее бы", callback_data="pfb_slower"),
    ]


def _strava_slots_line() -> str:
    """Строка «Сейчас свободно мест: X из 10» для экранов подключения Strava.
    Лимит — 10 athlete connections у неодобренного приложения."""
    from database import count_service_tokens
    cap = 10
    free = max(0, cap - count_service_tokens("strava"))
    return f"🎫 Сейчас свободно мест: {free} из {cap}."


async def cmd_connect_strava(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _mark_user_active_if_needed(user.id, user.full_name, user.username)
    auth_url = get_auth_url(user.id)
    keyboard = [[InlineKeyboardButton("🔗 Войти в Strava", url=auth_url)]]
    caption = (
        "Нажми кнопку и авторизуйся в Strava.\n\n"
        "После авторизации ты автоматически получишь сообщение в Telegram — ничего копировать не нужно.\n\n"
        "⚠️ Strava временно ограничена — подключение новых пользователей на проверке у Strava. "
        "Пока можно использовать Garmin или COROS (/connect_garmin, /connect_coros).\n\n"
        + _strava_slots_line()
    )
    badge = os.path.join(os.path.dirname(__file__), "..", "img",
                         "btn_strava_connect_with_orange_x2.png")
    try:
        with open(badge, "rb") as f:
            await update.message.reply_photo(
                photo=f, caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard))
    except FileNotFoundError:
        await update.message.reply_text(
            caption, reply_markup=InlineKeyboardMarkup(keyboard))


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


def _applied_threshold(db_user_id: int) -> str | None:
    """ПОРОГ, КОТОРЫЙ РЕАЛЬНО ИСПОЛЬЗУЕТСЯ в расчётах — темп threshold-зоны.
    Он может отличаться от ЛП в профиле: если якорем служит VO2max, зоны считаются
    от него, а приборный ЛП остаётся справочным. В рекомендацию и промт должен
    уходить именно применяемый, иначе ИИ видит два противоречащих числа."""
    try:
        z = zones.get_pace_zones(db_user_id) or {}
        return ((z.get("zones") or {}).get("threshold")) or None
    except Exception:
        return None


def _athlete_line(db_user_id: int) -> str:
    """Строка «МПК X · ПАНО Y/км @ Z» для рекомендации.
    ПАНО — применяемый (из зон), а не сырой ЛП из профиля.
    Пустая строка, если данных нет — блок тогда не рендерится."""
    p = get_user_profile(db_user_id) or {}
    parts = []
    if p.get("vo2max"):
        parts.append(f"МПК {p['vo2max']}")
    thr = _applied_threshold(db_user_id) or p.get("lactate_threshold_pace")
    if thr:
        lt = f"ПАНО {thr}/км"
        if p.get("lactate_threshold_hr"):
            lt += f" @ {p['lactate_threshold_hr']}"
        parts.append(lt)
    return " · ".join(parts)


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


def _speed_lines(db_user_id: int | None) -> list:
    """Строки со скоростями атлета: ПАНО, МПК, повторный, предел (200/400м).
    Пустой список, если зон ещё нет — блок тогда не рендерится."""
    if not db_user_id:
        return []
    try:
        z = zones.get_pace_zones(db_user_id)
    except Exception as e:
        logger.warning(f"Зоны для профиля uid={db_user_id} не получены: {e}")
        return []
    zz = (z or {}).get("zones") or {}
    if not zz:
        return []
    out = ["\nТвои скорости:"]
    try:
        _anchor = zones.resolve_anchor(get_user_profile(db_user_id))
    except Exception:
        _anchor = None
    if _anchor:
        out.append(f"Источник: {_anchor['text']} → VDOT {_anchor['vdot']:.1f}")
    if zz.get("threshold"):
        out.append(f"ПАНО (порог): {zz['threshold']} мин/км")
    if zz.get("interval"):
        out.append(f"МПК (интервальный): {zz['interval']} мин/км")
    if zz.get("repetition"):
        out.append(f"Повторный: {zz['repetition']} мин/км")
    if zz.get("repetition"):
        rtype = (z or {}).get("runner_type")
        parts = []
        for d in (100, 200, 300, 400):
            p = zones.repeat_pace_for_distance(zz["repetition"], d, rtype)
            if p:
                parts.append(f"{d}м {p}")
        if parts:
            out.append("Темп повторов: " + " · ".join(parts) + " мин/км")
        if rtype:
            out.append(f"Тип бегуна: {rtype}")
    return out


def _build_profile_text(profile: dict | None, db_user_id: int | None = None) -> str:
    if not profile or not any([profile.get("vo2max"), profile.get("lactate_threshold_pace"), profile.get("gender")]):
        return "Профиль не заполнен. Используй кнопки ниже чтобы добавить данные."
    lines = ["Твой профиль:\n"]
    if profile.get("gender"):
        lines.append(f"Пол: {'Мужской' if profile['gender'] == 'male' else 'Женский'}")
    if profile.get("vo2max"):
        tag = _vo2max_tag(profile)
        _prio = (profile.get("vo2max_resolved") or {}).get("priority") or "device"
        lock_icon = " 📌" if _prio == "manual" else ""
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
        # Если зоны считаются НЕ от этого ЛП — честно помечаем, что он справочный
        try:
            _anchor_kind = (zones.resolve_anchor(profile) or {}).get("kind", "")
        except Exception:
            _anchor_kind = ""
        if _anchor_kind and not _anchor_kind.startswith("lt"):
            lt += "\n⚠️ в расчёте не участвует — зоны считаются от VO2max (см. Источник ниже)"
        lines.append(lt)
    lines += _speed_lines(db_user_id)
    spec = profile.get("specialization")
    spec_label = SPECIALIZATIONS.get(spec) if spec else None
    lines.append(f"Специализация: {spec_label or 'Полумарафон (по умолчанию)'}")
    # 17.08.2026: актуальное восстановление в профиле — из кэшей (мгновенно, без сети)
    if db_user_id:
        try:
            _rl = []
            _snap = get_morning_caught(db_user_id)
            if _snap and _snap.get("caught"):
                _rl.append(
                    f"Утро {_snap.get('date') or '—'}: TR {_snap.get('tr') if _snap.get('tr') is not None else '—'} | "
                    f"BB {_snap.get('bb') if _snap.get('bb') is not None else '—'} | "
                    f"HRV {_snap.get('hrv') if _snap.get('hrv') is not None else '—'} | "
                    f"сон {_snap.get('sleep_h') if _snap.get('sleep_h') is not None else '—'}ч")
            from database import get_garmin_recovery_cache
            _gc = get_garmin_recovery_cache(db_user_id) or {}
            _gtr = _gc.get("training_readiness")
            if isinstance(_gtr, dict):
                _gtr = _gtr.get("score")
            if _gtr is not None:
                _gt = str(_gc.get("fetched_at") or "")[11:16]
                _rl.append(f"Последний синк{f' {_gt}' if _gt else ''}: TR {_gtr}")
            if _rl:
                lines.append("\n⚡ Восстановление:\n" + "\n".join(_rl))
        except Exception as e:
            logger.warning(f"profile recovery block: {e}")
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
        _prio = (p.get("vo2max_resolved") or {}).get("priority") or "device"
        lbl = "📌 VO2max: вручную" if _prio == "manual" else "📡 VO2max: из систем"
        lock_row.append(InlineKeyboardButton(lbl, callback_data="profile_toggle_vo2max_lock"))
    if p.get("lactate_threshold_pace"):
        _lprio = (p.get("lt_resolved") or {}).get("priority") or "device"
        lbl = "📌 ЛП: вручную" if _lprio == "manual" else "📡 ЛП: из систем"
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
        _build_profile_text(profile, db_user_id),
        reply_markup=_build_profile_keyboard(profile)
    )


# Режим формирования РЕКОМЕНДАЦИИ (Шаг 2). Анализ анонса (Шаг 1) всегда deep (админ).
_MODE_INFO = {
    "deep":  ("🧠", "Глубокий (ИИ)", "~3-7 мин",   "длинные рассуждения, макс. качество"),
    "smart": ("⚖️", "Умный (ИИ)",    "~1-5 мин",   "рассуждения покороче, баланс"),
    "fast":  ("🪶", "Лёгкий (ИИ)",   "~10 сек",    "без рассуждений, быстрый ответ"),
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
        "🔔 Настройка уведомлений:\n\n"
        f"{mark('notify_interval')} Вечер накануне вт/пт (20:00) — рекомендация группы\n"
        f"{mark('notify_morning_interval')} Утро вт/пт (07:00) — проверка готовности\n"
        f"{mark('notify_long')} Вечер накануне Long Run (20:00)\n"
        f"{mark('notify_morning_long')} Утро воскресенья (07:30)"
    )


def _build_notifications_keyboard(prefs: dict) -> InlineKeyboardMarkup:
    def lbl(key, title):
        on = (prefs or {}).get(key, True)
        action = f"notif_off_{key}" if on else f"notif_on_{key}"
        return InlineKeyboardButton(f"{'✅' if on else '❌'} {title} — {'[Выкл]' if on else '[Вкл]'}", callback_data=action)
    return InlineKeyboardMarkup([
        [lbl("notify_interval", "Вечер вт/пт")],
        [lbl("notify_morning_interval", "Утро вт/пт")],
        [lbl("notify_long", "Вечер Long Run")],
        [lbl("notify_morning_long", "Утро воскресенья")],
        _settings_nav(),
    ])


def _build_mailing_report(date: str, sent: list | None = None) -> str:
    """Текст отчёта по рассылке за дату: сводка по группам + поимённо.
    19.08.2026: единая точка — зовётся из scheduled_evening и из /mailing.
    sent — список (telegram_id, name, username) фактически отправленных;
    если передан — добавляется блок «Без рекомендации»."""
    recs = get_recommendations_for_date(date) if date else []
    if not recs:
        return ""
    groups: dict[str, list] = {}
    for r in recs:
        groups.setdefault(str(r["recommended_group"] or "—"), []).append(r)
    summary = " · ".join(f"гр{g}: {len(lst)}" for g, lst in groups.items())
    lines = [f"📊 Группы: {summary}"]
    if sent is not None:
        rec_tids = {r.get("telegram_id") for r in recs}
        no_rec = [(n, u) for tid, n, u in sent if tid not in rec_tids]
        if no_rec:
            refs = ", ".join(f"@{u}" if u else (n or "—") for n, u in no_rec)
            lines.append(f"📭 Без рекомендации ({len(no_rec)}): {refs}")
    if BROADCAST_REPORT_DETAILED:
        for g, lst in groups.items():
            lines.append(f"\n▸ Группа {g}")
            for r in lst:
                rs = r["evening_recovery_score"]
                mark = "↓" if r["lowered_by_recovery"] else ""
                nick = f" (@{r['username']})" if r.get("username") else ""
                lines.append(f"   {r['name']}{nick} (rec={rs if rs is not None else '—'}{mark})")
    return "\n".join(lines)


async def cmd_mailing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mailing [YYYY-MM-DD] — заново собрать отчёт по рассылке (19.08.2026).
    Тот же вид, что приходит по завершении вечерней рассылки: группы + поимённо.
    Без аргумента — дата последней рассылки."""
    user = update.effective_user
    if user.id not in ADMIN_TELEGRAM_IDS:
        return
    date = (context.args[0] if context.args else "").strip()
    if not date:
        from database import get_stats_overview
        lr = (get_stats_overview() or {}).get("last_reco") or {}
        date = lr.get("date") or ""
    recs_report = _build_mailing_report(date)
    if not recs_report:
        await update.message.reply_text(
            f"Нет рекомендаций за {date or '—'}. Формат: /mailing 2026-08-18")
        return
    text = f"📨 <b>Рассылка {date}</b>\n{recs_report}"
    for i in range(0, len(text), 3900):
        await update.message.reply_text(text[i:i + 3900], parse_mode="HTML")


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
        "/workout — рекомендация группы для вт/пт тренировки (можно с датой: /workout 0901)\n"
        "/long — рекомендация для воскресного Long Run\n"
        "/morning — утренняя проверка восстановления\n"
        "/report — разбор прошедшей тренировки: графики и анализ от ИИ\n"
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
            "/cleanup — убрать за теми, кто заблокировал бота: отзыв Strava/Polar\n"
            "/prompt — последний промпт к модели\n"
            "/debug — разбор последней тренировки\n"
            "/debug_long — разбор последнего Long Run\n"
            "/ratings — последние оценки рекомендаций\n"
            "/feedbacks — последние сообщения обратной связи\n"
            "/analyze_test — тестовый разбор анонса (без записи в базу)\n"
            "/brief — анализ с режимами (без даты — последний, или /brief 20260804)\n"
            "/preprocess_mode — режим анализа тренировок (deep/smart)\n"
            "/test_workout — тест Шага 2 (рекомендация группы) на твоих данных\n"
            "/test_long — тест Шага 2 для длительной на твоих данных\n"
            "/reanalyze — боевой переразбор анонса (запись в базу + эталоны + бриф)\n"
            "/show_analyze — показать последний Шаг 1 из базы\n"
            "/b — вариант B для себя\n"
            "/b_user — вариант B для выбранного пользователя\n"
            "/a_user — вариант A для выбранного пользователя\n"
            "/w_user — реальный путь пользователя (его ai_mode: B или A)\n"
            "/w_user_light — то же, но принудительно в лёгком режиме (fast)\n"
            "/report_user — разбор тренировки выбранного пользователя (/report_user 18886572975 — конкретная по id/маске)\n"
            "/l_user — лонг для выбранного пользователя\n"
            "/p_b — промпт варианта B для себя (можно с датой: /p_b 0901)\n"
            "/p_b_user — промпт варианта B для выбранного пользователя (можно с датой: /p_b_user 0901)\n"
            "/p_a — промпт варианта A для себя\n"
            "/p_a_user — промпт варианта A для выбранного пользователя\n"
            "/p_analyze — промпт Шага 1 (анализ анонса)\n"
            "/activity — активность по дням и топ действий за 14 дней"
            "\n/mailing — отчёт по рассылке поимённо (без даты — последняя; /mailing 2026-08-18)"
            "\n/brief — бриф режимов из кэша (/brief 20260804 — за дату); если режимов нет — кнопка «Сгенерировать режимы» (deep, без полного переанализа)"
            "\n/brief_p — показать промт режимов целиком (/brief_p 20260904 — за дату), ИИ не зовётся"
            "\n/rebrief — ПРИНУДИТЕЛЬНО пересобрать режимы заново (/rebrief 20260804), без переанализа анонса"
            "\n/resend_evening — дослать вечернюю тем, кому не ушла (/resend_evening 20260814 [fast|smart|deep])"
            "\n/msg_user <id> <текст> — написать юзеру от имени бота"
            "\n/msg_service — написать всем, у кого подключён выбранный сервис"
            "\n/profile_user — посмотреть профиль выбранного пользователя"
            "\n/last — разбор последней выполненной тренировки (графики факт vs план; /last dark — тёмная тема)"
            "\n/report — ИИ-анализ последней тренировки (/report DD_20260612 — выбрать; /report data — сырой пакет+промпт)"
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
    """Статистика бота: воронка данных + матрица подключений (только для админов).
    Все числа — по единым определениям (get_stats_overview): активные, resolved-поля v2."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    from database import get_stats_overview
    s = get_bot_stats()          # запросы/оценки за период — как были
    o = get_stats_overview()     # воронка/матрица — единые определения

    # Матрица пересечений: диагональ — всего по сервису, клетки — вместе
    order = [("strava", "Str"), ("garmin", "Gar"), ("coros", "Cor"),
             ("coros_mcp", "CoM"), ("whoop", "Whp"), ("polar", "Pol")]
    svc = o["services"]
    mrows = ["     " + "".join(f"{lbl:>5}" for _, lbl in order) + f"{'один':>7}"]
    for i, (k1, l1) in enumerate(order):
        cells = []
        for j, (k2, _) in enumerate(order):
            if j < i:
                cells.append(f"{'':>5}")
            elif j == i:
                cells.append(f"{len(svc.get(k1, set())):>5}")
            else:
                n = len(svc.get(k1, set()) & svc.get(k2, set()))
                cells.append(f"{(n if n else '·'):>5}")
        others = set().union(*(svc.get(k2, set()) for k2, _ in order if k2 != k1)) \
            if len(order) > 1 else set()
        only_n = len(svc.get(k1, set()) - others)
        mrows.append(f"{l1:<5}" + "".join(cells) + f"{only_n:>7}")
    matrix = "\n".join(mrows)

    reco_line = "—"
    lr = o.get("last_reco")
    from database import get_pace_feedback_last
    _pfd, _pfc, _pfg = get_pace_feedback_last((lr or {}).get("date"))

    def _pf3(c):
        return (f"🚀{c.get('faster', 0)} 🎯{c.get('ok', 0)} 🐢{c.get('slower', 0)}")

    if lr:
        # 19.08.2026: распределение по группам — столбиками; справа — ответы кнопок темпа
        _items = sorted(lr["by_group"].items(),
                        key=lambda kv: float(str(kv[0]).replace(",", "."))
                        if str(kv[0]).replace(",", ".").replace(".", "").isdigit() else 99)
        _mx = max((n for _, n in _items), default=0) or 1
        _w = max((len(f"гр{g}") for g, _ in _items), default=3)

        def _tail(g):
            c = _pfg.get(str(g))
            if not c:
                return ""
            parts = [f"{e}{c[k]}" for e, k in (("🚀", "faster"), ("🎯", "ok"), ("🐢", "slower")) if c.get(k)]
            return " ".join(parts)

        _left = [f"{('гр' + str(g)).ljust(_w)} {'█' * max(1, round(n * 14 / _mx))} {n}"
                 for g, n in _items]
        _lw = max(len(x) for x in _left) + 2
        by_g = "<pre>" + "\n".join(
            (l.ljust(_lw) + _tail(g)).rstrip()
            for l, (g, _) in zip(_left, _items)) + "</pre>"
        reminders = max(lr["subscribed"] - lr["rec_total"], 0)
        reco_line = (f"{lr['wtype']}, {lr['date']}: получателей {lr['subscribed']} = "
                     f"рекомендаций {lr['rec_total']} + напоминаний {reminders}\n"
                     f"{by_g}")

    if _pfd and _pfc:
        _pfn = sum(_pfc.values())
        pfb_line = f"Темп по ощущениям: {_pf3(_pfc)} · ответили {_pfn}\n\n"
    else:
        pfb_line = ""
    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Пользователи: {o['total']} — активных {o['active']}, заблокировали {o['blocked']}\n"
        f"Новых за 7 дней: {s['new_7d']} · активных за 7 дней: {s['active_7d']}\n\n"
        "📡 <b>Данные</b> (по активным):\n"
        f"с трекером {o['tracker_users']} + только профиль {o['profile_only']} "
        f"+ пусто {o['empty']} = {o['active']}\n"
        f"якорь для зон: {o['anchor_users']} · зоны посчитаны: {o['zones_users']}\n"
        f"готовность (утро ловится): за 7 дней {o.get('morning_7d', 0)} · сегодня {o.get('morning_today', 0)}\n\n"
        f"<b>Подключения</b> (диагональ — всего, клетки — вместе):\n"
        f"<pre>{matrix}</pre>\n"
        f"📨 <b>Последняя рассылка</b>:\n{reco_line}\n\n"
        f"{pfb_line}"
        "Запросы за 7 дней (в скобках — уникальных):\n"
        f"📋 /workout: {s['workout_7d']} ({s.get('workout_users_7d', 0)})\n"
        f"🕐 /long: {s['long_7d']} ({s.get('long_users_7d', 0)})\n"
        f"☀️ /morning: {s['morning_7d']} ({s.get('morning_users_7d', 0)})\n"
        f"📊 /report: {s.get('report_7d', 0)} ({s.get('report_users_7d', 0)})\n\n"
        f"⭐ Средняя оценка: {s.get('avg_rating') or '—'}/10 (за 30 дней)\n"
        f"📊 Оценок получено: {s.get('ratings_30d', 0)}\n"
        f"💬 Обратной связи: {s.get('feedback_total', 0)} "
        f"(баги: {s.get('feedback_bugs', 0)}, идеи: {s.get('feedback_features', 0)})"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu_new")]]))


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
    # Текст режется на куски по 4096 — кнопку вешаем только на последний.
    chunks = [text[i:i + 4096] for i in range(0, len(text), 4096)]
    for n, chunk in enumerate(chunks, start=1):
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            "🏠 Главное меню", callback_data="main_menu_new")]]) if n == len(chunks) else None
        await update.message.reply_text(chunk, reply_markup=markup)


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
        ("coros_mcp", "⌚ COROS без пароля"),
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

    # Неактивных (заблокировавших бота) исключаем из всех категорий выше.
    inactive = get_inactive_users()
    inactive_tids = {row[0] for row in inactive}
    only_profile = [r for r in only_profile if r[0] not in inactive_tids]
    nothing = [r for r in nothing if r[0] not in inactive_tids]

    lines = ["📊 Пользователи по сервисам:\n"]
    for svc, label in service_defs:
        rows = [r for r in service_users[svc] if r[0] not in inactive_tids]
        refs = ", ".join(_fmt_user_ref(n, u) for _, n, u in rows)
        lines.append(f"{label} ({len(rows)}): {refs or '—'}")

    refs = ", ".join(_fmt_user_ref(n, u) for _, n, u in only_profile)
    lines.append(f"\n👤 Только профиль ({len(only_profile)}): {refs or '—'}")

    refs = ", ".join(_fmt_user_ref(n, u) for _, n, u in nothing)
    lines.append(f"❌ Ничего не подключено ({len(nothing)}): {refs or '—'}")

    if inactive:
        refs = ", ".join(_fmt_user_ref(n, u) for _, n, u in inactive)
        lines.append(f"\n💤 Неактивных (заблокировали бота) ({len(inactive)}): {refs}")

    text = "\n".join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cleanup — убрать за теми, кто заблокировал бота мимо кнопки «Отключить».

    Сначала только показывает список; отзыв — по кнопке подтверждения (админ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    items = _blocked_with_tokens()
    if not items:
        await update.message.reply_text(
            "Чисто: у заблокировавших бота подключений не осталось.")
        return
    lines = [f"🧹 Заблокировали бота, подключения остались: {len(items)}\n"]
    for _uid, who, svcs, since in items:
        lines.append(f"• @{who} — {', '.join(svcs)} (с {since or '—'})")
    lines.append("\nОтозвать доступ у сервисов и удалить ключи?"
                 "\nПрофиль, зоны и история останутся на месте.")
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("🧹 Отозвать и очистить", callback_data="cleanup_do"),
        InlineKeyboardButton("❌ Отмена", callback_data="cleanup_cancel"),
    ]]))


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


async def cmd_analyze_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тестовый прогон Шага 1 без записи в БД (только для админов)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    await update.message.reply_text(
        "🧪 Тестовый разбор — результат НЕ сохраняется в базу.\n"
        "Какую тренировку разобрать?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚡ Интервальная (вт/пт)", callback_data="analyze_test_interval"),
            InlineKeyboardButton("🕐 Long Run (вс)",        callback_data="analyze_test_long"),
        ]])
    )


async def _run_analyze_test(workout: dict, query, context: ContextTypes.DEFAULT_TYPE):
    """Тестовый прогон Шага 1: тот же разбор и тот же вывод, НО БЕЗ ЗАПИСИ в БД.
    Боевые пути (шедулер, /reanalyze) используют то же ядро + _store_analysis.
    """
    if not (workout.get("raw_text") or ""):
        await query.edit_message_text("❌ Не удалось получить текст поста для разбора.")
        return

    mode = get_preprocess_mode()
    await query.edit_message_text(
        f"🧪 Тестовый разбор через DeepSeek (режим {mode})...\nМожет занять 1-2 минуты."
    )

    result = await _analyze_core(workout, mode)
    if not result:
        await query.edit_message_text("❌ Разбор не удался (пустой ответ модели). Попробуй ещё раз.")
        return

    banner = ("🧪 ТЕСТОВЫЙ ПРОГОН — в базу НЕ записано, эталоны не обновлены.\n"
              "Для боевого обновления — /reanalyze\n\n")
    text = banner + _format_analysis_result(result, mode)
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


async def _reanalyze_one(workout: dict, mode: str, context=None) -> str:
    """Боевой переразбор одного анонса (игнор idempotency): то же ядро,
    что у шедулера, + единая запись (анализ + эталоны) + обновлённый бриф.
    Возвращает готовый вывод разбора для админа (тот же, что у тестового прогона).
    """
    label = "🕐 Long Run" if workout.get("workout_type") == "long" else "⚡ Интервальная"
    date_fmt = workout.get("workout_date", "—")

    result = await _analyze_core(workout, mode)
    if not result:
        return f"{label} — {date_fmt}: ❌ разбор не удался (пустой ответ модели)"

    _store_analysis(result, workout, mode)
    logger.info(f"reanalyze: post_id={workout.get('post_id')} обновлён вручную "
                f"(type={result.get('workout_type')}, valid={result.get('is_valid')})")

    # Обновлённый бриф режимов (Шаг 1.5) — только интервальные, не роняет переразбор.
    if context is not None and result.get("workout_type") != "long":
        try:
            import announce_brief
            brief = await asyncio.to_thread(
                announce_brief.build_admin_brief, result, workout.get("post_id"), "deep")
            if brief:
                for i in range(0, len(brief), 4096):
                    await _notify_admin(context.bot, brief[i:i + 4096])
        except Exception as e:
            logger.warning(f"reanalyze: announce_brief error: {e}")

    return "💾 ЗАПИСАНО В БАЗУ (анализ + эталоны)\n\n" + _format_analysis_result(result, mode)


_CAP_OLD = (
    "Группу с финишем быстрее — risk, % ≤ 10.\n"
    "Исключение: если повторы короткие (≤200 м) и в задании есть прогрессия "
    "темпа, выход быстрее потолка допустим на завершающих повторах — не более "
    "чем на пятой части их общего числа; такая группа не risk — оценивай её "
    "по основной части повторов.\n"
)
_CAP_MID = (
    "Все рабочие отрезки не должны быть быстрее этого темпа — для этих групп "
    "risk, % ≤ 10. Если в задании есть прогрессия темпа, сравнивай с потолком "
    "не самый быстрый край диапазона группы, а середину диапазона — темп, "
    "который держится на основной части повторов.\n"
)


async def cmd_shadow_caps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/shadow_caps [limit] — теневой А/Б по потолку скорости (21.08.2026).
    По каждому юзеру с зонами — 4 вызова: deep с потолком / deep без /
    fast с потолком / fast без. НИЧЕГО не шлёт пользователям и не пишет в базу:
    результат ложится в data/shadow_caps.txt и короткой сводкой админу."""
    user = update.effective_user
    if user.id not in ADMIN_TELEGRAM_IDS:
        return
    import time as _time
    import os as _os_sc
    # 21.08.2026: пути — АБСОЛЮТНЫЕ от расположения кода: у службы другой рабочий
    # каталог, и относительный data/ молча не создавался.
    _data_dir = _os_sc.path.join(_os_sc.path.dirname(_os_sc.path.dirname(
        _os_sc.path.abspath(__file__))), "data")
    _os_sc.makedirs(_data_dir, exist_ok=True)
    _raw_path = _os_sc.path.join(_data_dir, "shadow_caps_raw.jsonl")
    _sum_path = _os_sc.path.join(_data_dir, "shadow_caps.txt")
    limit = 0
    if context.args:
        try:
            limit = int(context.args[0])
        except ValueError:
            limit = 0

    users = [(tid, name) for tid, name, _un, has in get_all_users_with_status() if has]
    if limit:
        users = users[:limit]
    TAGS = ("deep~", "fast~")
    msg = await update.message.reply_text(
        f"🧪 Теневой прогон потолка: {len(users)} человек × {len(TAGS)} вызова "
        f"({', '.join(TAGS)}). Рассылки не будет.")

    _sem = asyncio.Semaphore(5)
    rows: list[str] = []
    # 21.08.2026: попарная матрица расхождений между сценариями
    pair_diff: dict[tuple, int] = {}
    pair_cases: dict[tuple, list] = {}
    t_deep = t_fast = 0.0
    done = 0

    async def _one(tid: int, name: str):
        nonlocal t_deep, t_fast, done
        async with _sem:
            try:
                db_user_id = get_or_create_user(tid, name)
                analysis, user_data, workout_dict, _ = await _build_analysis_and_user_data(db_user_id)
                if analysis is None:
                    return
                res: dict[str, tuple] = {}
                for tag, mode, caps in (("deep~", "deep", "mid"),
                                        ("fast~", "fast", "mid")):
                    prompt, _ctx = await _build_variant_b_prompt(
                        db_user_id, analysis, user_data, workout_dict,
                        with_ceilings=(caps is not False))
                    if caps == "mid":
                        if _CAP_OLD in prompt:
                            prompt = prompt.replace(_CAP_OLD, _CAP_MID)
                        else:
                            logger.warning("shadow_caps: блок потолка не найден для mid")
                    _t0 = _time.time()
                    advice = await asyncio.to_thread(claude_advisor.ask_groq, prompt, mode)
                    _dt = _time.time() - _t0
                    grp = str(((advice or {}).get("advice") or {}).get("recommended_group") or "—")
                    # 21.08.2026: сохраняем ПОЛНЫЙ ответ каждого вызова — чтобы часовой
                    # прогон не пришлось повторять ради деталей (JSONL, одна строка = один вызов)
                    try:
                        import json as _json_sc
                        with open(_raw_path, "a", encoding="utf-8") as _f:
                            _f.write(_json_sc.dumps({
                                "user": name, "uid": db_user_id, "tag": tag,
                                "mode": mode, "ceilings": caps, "seconds": round(_dt, 1),
                                "advice": (advice or {}).get("advice"),
                                "stats": (advice or {}).get("stats"),
                            }, ensure_ascii=False) + "\n")
                    except Exception as _e:
                        logger.warning(f"shadow_caps raw: {_e}")
                    res[tag] = (grp, _dt)
                    if mode == "deep":
                        t_deep += _dt
                    else:
                        t_fast += _dt
                    await asyncio.sleep(0)
                for _i, _a in enumerate(TAGS):
                    for _b in TAGS[_i + 1:]:
                        ga = res.get(_a, ("—", 0))[0]
                        gb = res.get(_b, ("—", 0))[0]
                        if ga != gb:
                            pair_diff[(_a, _b)] = pair_diff.get((_a, _b), 0) + 1
                            pair_cases.setdefault((_a, _b), []).append(f"{name[:18]} {ga}≠{gb}")
                rows.append(
                    f"{name[:22]:<22} "
                    + "  ".join(f"{k}={res.get(k, ('—', 0))[0]}({res.get(k, ('—', 0))[1]:.0f}с)"
                                for k in ("deep~", "fast~")))
                done += 1
            except Exception as e:
                rows.append(f"{name[:22]:<22} ОШИБКА: {str(e)[:60]}")

    await asyncio.gather(*[_one(t, n) for t, n in users])

    matrix = ["Матрица расхождений (сколько человек из " + str(done) + " получили РАЗНЫЕ группы):"]
    for _i, _a in enumerate(TAGS):
        for _b in TAGS[_i + 1:]:
            n = pair_diff.get((_a, _b), 0)
            ex = "; ".join(pair_cases.get((_a, _b), [])[:5])
            matrix.append(f"  {_a} vs {_b}: {n}" + (f"   → {ex}" if ex else ""))
    header = (f"Теневой прогон потолка — участников: {done}\n"
              + "\n".join(matrix) + "\n"
              f"Среднее время deep: {t_deep / max(done * 2, 1):.0f}с · fast: {t_fast / max(done * 2, 1):.0f}с\n")
    try:
        with open(_sum_path, "w", encoding="utf-8") as f:
            f.write(header + "\n" + "\n".join(rows))
    except Exception as e:
        logger.warning(f"shadow_caps file: {e}")
    await msg.edit_text(header + f"\nФайлы: {_sum_path} и {_raw_path}")
    body = "\n".join(rows)
    for i in range(0, len(body), 3900):
        await update.message.reply_text(f"<pre>{body[i:i + 3900]}</pre>", parse_mode="HTML")


async def cmd_rebrief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rebrief [YYYYMMDD] — ПРИНУДИТЕЛЬНО пересобрать режимы (20.08.2026).
    В отличие от /brief (рендер из кэша) всегда зовёт ИИ заново и перезаписывает
    modes в анализе; полный переанализ анонса НЕ делается."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    import json as _json
    arg = (context.args[0] if context.args else "").strip()
    wdate = None
    if arg:
        wdate = _parse_cmd_date(arg)
        if not wdate:
            await update.message.reply_text(
                "Формат: /rebrief 20260804 или /rebrief 0804 (без даты — последний)")
            return

    row = _analysis_row(wdate)
    if not row:
        await update.message.reply_text(f"Анализ {'за ' + wdate if wdate else ''} не найден.")
        return
    post_id, found_date, ajson = row
    try:
        result = _json.loads(ajson or "{}")
    except Exception:
        await update.message.reply_text("Анализ не распарсился.")
        return

    msg = await update.message.reply_text(
        f"🧭 Пересобираю режимы за {found_date} (deep, 2-3 минуты)…")
    try:
        import announce_brief
        text = await asyncio.to_thread(
            announce_brief.build_admin_brief, result, post_id, "deep")
    except Exception as e:
        await msg.edit_text(f"Ошибка генерации режимов: {str(e)[:200]}")
        return
    if not text:
        await msg.edit_text("Режимы не сгенерировались (пустой ответ ИИ).")
        return
    await msg.edit_text(text[:4096])
    for i in range(4096, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Анализ (РЦС + режимы) из истории: /brief — последний,
    /brief 20260804 — за конкретную дату. Рендер из кэша analyzed_json["modes"],
    без обращения к ИИ. Если режимов нет (старые анализы) — кнопка генерации."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    import json as _json
    arg = (context.args[0] if context.args else "").strip()
    wdate = None
    if arg:
        wdate = _parse_cmd_date(arg)
        if not wdate:
            await update.message.reply_text(
                "Формат: /brief 20260804 или /brief 0804 (без даты — последний)")
            return

    row = _analysis_row(wdate)
    if not row:
        await update.message.reply_text(
            f"Анализ {'за ' + wdate if wdate else ''} не найден.")
        return
    post_id, found_date, ajson = row
    try:
        result = _json.loads(ajson or "{}")
    except Exception:
        await update.message.reply_text("Анализ не распарсился.")
        return

    modes = result.get("modes")
    if not modes:
        await update.message.reply_text(
            f"Анализ за {found_date} есть, но режимы для него не считались.\n"
            "Можно сгенерировать сейчас (deep, 2-3 минуты) — результат сохранится в анализе.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🧭 Сгенерировать режимы",
                                     callback_data=f"brief_gen_{post_id}"),
            ]]))
        return

    import announce_brief
    text = announce_brief.format_brief(result, modes)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def cmd_brief_p(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/brief_p [YYYYMMDD] — показать промт режимов целиком (admin).
    Без даты — последний анализ. ИИ НЕ зовётся: это ровно тот текст,
    который уходит в ИИ при сборке режимов (announce_brief.build_modes_prompt)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    import json as _json
    arg = (context.args[0] if context.args else "").strip()
    wdate = None
    if arg:
        wdate = _parse_cmd_date(arg)
        if not wdate:
            await update.message.reply_text(
                "Формат: /brief_p 20260904 или /brief_p 0904 (без даты — последний)")
            return
    row = _analysis_row(wdate)
    if not row:
        await update.message.reply_text(
            f"Анализ {'за ' + wdate if wdate else ''} не найден.")
        return
    _post_id, found_date, ajson = row
    try:
        result = _json.loads(ajson or "{}")
    except Exception:
        await update.message.reply_text("Анализ не распарсился.")
        return
    import announce_brief
    prompt = announce_brief.build_modes_prompt(result)
    header = f"🧭 Промт режимов за {found_date} ({len(prompt)} знаков):\n\n"
    text = header + prompt
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


def _analysis_row(wdate: str | None):
    """(post_id, workout_date, analyzed_json) валидного интервального анализа:
    за дату или последний по дате тренировки."""
    from database import get_connection
    base = ("SELECT post_id, workout_date, analyzed_json FROM workout_analysis "
            "WHERE is_valid = 1 AND workout_type = 'interval' ")
    with get_connection() as conn:
        if wdate:
            return conn.execute(base + "AND workout_date = ? ORDER BY updated_at DESC LIMIT 1",
                               (wdate,)).fetchone()
        return conn.execute(base + "ORDER BY workout_date DESC LIMIT 1").fetchone()


async def cmd_reanalyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Боевой переразбор анонса с записью в базу: анализ + эталоны + бриф (админ)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    await update.message.reply_text(
        "💾 Боевой переразбор — результат ЗАПИШЕТСЯ в базу (анализ + эталоны).\n"
        "Какую тренировку разобрать?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚡ Интервальная (вт/пт)", callback_data="reanalyze_interval"),
            InlineKeyboardButton("🕐 Long Run (вс)",        callback_data="reanalyze_long"),
        ]])
    )


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
            "После авторизации ты автоматически получишь сообщение в Telegram — ничего копировать не нужно.\n\n"
            + _strava_slots_line(),
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

    elif query.data == "connect_coros_oauth_btn":
        import coros_oauth
        auth_url = coros_oauth.build_auth_url(user.id)
        await query.edit_message_text(
            "Подключение COROS без пароля\n\n"
            "Нажми кнопку ниже — откроется страница самого COROS. Введи там почту "
            "и пароль от COROS и нажми Authorize. Бот пароль не видит и не хранит.\n\n"
            "Важно: поставь ОБЕ галочки — вторая открывает доступ к уже записанным "
            "тренировкам.\n\n"
            "Ссылка действует 15 минут.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⌚ Открыть страницу COROS", url=auth_url)]]
            ),
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
        revoked = await _revoke_service(db_user_id, svc)
        delete_token(db_user_id, svc)
        done_msg = _svc_done_msg(svc)
        logger.info(f"Сервис {svc} отключён для user {db_user_id} "
                    f"(отзыв у сервиса: {'да' if revoked else 'нет'})")
        await query.edit_message_text(
            f"✅ {done_msg}.",
            reply_markup=_build_screen3_keyboard(db_user_id)
        )

    elif query.data == "cleanup_cancel":
        await query.edit_message_text("Отменено — ничего не изменено.")

    elif query.data == "cleanup_do":
        if user.id not in ADMIN_TELEGRAM_IDS:
            return
        items = _blocked_with_tokens()
        await query.edit_message_text(f"🧹 Отзываю доступ… ({len(items)} чел.)")
        ok = fail = 0
        report = []
        for uid, who, svcs, _since in items:
            for s in svcs:
                if s not in ("strava", "polar"):
                    delete_token(uid, s)
                    ok += 1
                    report.append(f"✅ @{who} — {s}: ключ удалён")
                    continue
                if await _revoke_service(uid, s):
                    delete_token(uid, s)
                    ok += 1
                    report.append(f"✅ @{who} — {s}: отозвано, ключ удалён")
                else:
                    fail += 1
                    report.append(f"⚠️ @{who} — {s}: не вышло, ключ оставлен")
        report.append(f"\nИтого: отозвано {ok}, не вышло {fail}")
        logger.info(f"cleanup: отозвано {ok}, ошибок {fail}")
        await query.edit_message_text("\n".join(report)[:4000])

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
                    save_vo2max_device(db_user_id, float(vo2max_val), "garmin")
                    if not (profile or {}).get("vo2max_locked"):
                        save_user_profile(db_user_id, vo2max=float(vo2max_val), vo2max_source="auto")
                    garmin_parts.append(f"VO2max {float(vo2max_val):.0f}")
                if not isinstance(lt, Exception) and lt:
                    save_lt_device(db_user_id, lt["pace"], lt.get("hr"), "garmin")
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
                    save_vo2max_device(db_user_id, float(coros_vo2max), "coros")
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
                    save_vo2max_device(db_user_id, float(polar_vo2max), "polar")
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
            _build_profile_text(profile, db_user_id),
            reply_markup=_build_profile_keyboard(profile)
        )

    elif query.data == "profile_set_vo2max":
        context.user_data["awaiting_profile"] = "set_vo2max"
        await query.edit_message_text(
            "📊 <b>VO2max</b> — максимальное потребление кислорода, мл/кг/мин.\n"
            "Где взять: часы (Garmin — «МПК») или лабораторный тест. У любителей обычно 35–65.\n"
            "От него считаются все твои темповые зоны, если не задан лактатный порог.\n\n"
            "Например: 53",
            parse_mode="HTML"
        )

    elif query.data == "profile_set_lactate":
        context.user_data["awaiting_profile"] = "set_lactate_pace"
        await query.edit_message_text(
            "🏃 <b>Лактатный порог (ПАНО)</b> — темп, который ты удерживаешь без развала "
            "примерно 50–60 минут подряд (≈ темп на 10 км у большинства).\n"
            "Это НЕ темп интервалов и не темп с короткой гонки — такая ошибка сделает все зоны "
            "слишком быстрыми.\n"
            "Важно: если порог задан вручную, зоны считаются ОТ НЕГО, а не от VO2max.\n\n"
            "Введи темп (мин:сек на км), например: 4:17",
            parse_mode="HTML"
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
            f"✅ Пол сохранён: {gender_label}\n\n{_build_profile_text(profile, db_user_id)}",
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
            f"✅ Специализация сохранена: {SPECIALIZATIONS[spec]}\n\n{_build_profile_text(profile, db_user_id)}",
            reply_markup=_build_profile_keyboard(profile)
        )

    elif query.data in ("profile_toggle_vo2max_lock", "profile_toggle_lactate_lock"):
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        profile = get_user_profile(db_user_id)
        if query.data == "profile_toggle_vo2max_lock":
            cur = ((profile or {}).get("vo2max_resolved") or {}).get("priority") or "device"
            new_prio = "device" if cur == "manual" else "manual"
            set_vo2max_priority(db_user_id, new_prio)
            if new_prio == "manual" and not (((profile or {}).get("vo2max_resolved") or {})
                                             .get("manual", {}) or {}).get("value"):
                note = ("📌 Приоритет: вручную. Ручное значение ещё не задано — введи через "
                        "«Указать VO2max», пока используется значение из систем.")
            else:
                note = ("📌 Используется значение, введённое вручную." if new_prio == "manual"
                        else "📡 Используется значение из систем (Garmin/COROS/Polar).")
        else:
            cur = ((profile or {}).get("lt_resolved") or {}).get("priority") or "device"
            new_prio = "device" if cur == "manual" else "manual"
            set_lt_priority(db_user_id, new_prio)
            if new_prio == "manual" and not (((profile or {}).get("lt_resolved") or {})
                                             .get("manual", {}) or {}).get("pace"):
                note = ("📌 Приоритет ЛП: вручную. Ручное значение ещё не задано — введи через "
                        "«Лактатный порог», пока используется значение из систем.")
            else:
                note = ("📌 ЛП: используется значение, введённое вручную." if new_prio == "manual"
                        else "📡 ЛП: используется значение из систем (Garmin).")
        profile = get_user_profile(db_user_id)
        await query.edit_message_text(
            f"{note}\n\n{_build_profile_text(profile, db_user_id)}",
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
                    f"✅ <b>Тренировка отправлена в часы!</b>\n\n"
                    f"⌚ {name.removesuffix('.json')}\n\n"
                    f"Синхронизируй часы — тренировка появится в списке тренировок.",
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
            # 17.08.2026: грузим СРАЗУ при выборе группы — промежуточная кнопка убрана
            # (нажатие номера группы = решение, второе подтверждение излишне).
            db_user_id = get_or_create_user(user.id, user.full_name, user.username)
            if not get_token(db_user_id, "garmin"):
                await context.bot.send_message(
                    user.id,
                    f"⌚ <b>{fname}</b> готова, но Garmin не подключён.\n"
                    "Используй /connect_garmin и выбери группу заново.",
                    parse_mode="HTML",
                    reply_markup=_add_main_menu_btn(None))
                return
            from garmin import upload_workout as garmin_upload_workout
            ok = await garmin_upload_workout(db_user_id, wkt)
            if ok:
                await context.bot.send_message(
                    user.id,
                    f"✅ <b>Тренировка отправлена в часы!</b>\n\n"
                    f"⌚ {fname.removesuffix('.json')}\n\n"
                    f"Синхронизируй часы — тренировка появится в списке тренировок.",
                    parse_mode="HTML",
                    reply_markup=_add_main_menu_btn(None))
            else:
                await context.bot.send_message(
                    user.id,
                    f"❌ Не удалось загрузить {fname} в Garmin — попробуй ещё раз через минуту.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔁 Повторить", callback_data="fit_up"),
                    ]]))
        except Exception as e:
            logger.error(f"garmin_grp_{group_num} error for {user.id}: {e}", exc_info=True)
            await context.bot.send_message(
                user.id,
                f"❌ Ошибка генерации тренировки: {type(e).__name__}: {e}",
                reply_markup=_add_main_menu_btn(None),
            )

    elif query.data == "help":
        back_btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Видеоинструкция", callback_data="video_manual")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
        await query.edit_message_text(_build_help_text(user.id in ADMIN_TELEGRAM_IDS), reply_markup=back_btn)

    elif query.data == "video_manual":
        # 17.08.2026: раздел видеоинструкции — file_id хранится в bot_settings,
        # само видео живёт на серверах Telegram (админ шлёт ролик боту — он запоминает).
        from database import get_connection as _gc_vm
        with _gc_vm() as _conn_vm:
            _row_vm = _conn_vm.execute(
                "SELECT value FROM bot_settings WHERE key = 'video_manual_file_id'").fetchone()
        if _row_vm and _row_vm[0]:
            await context.bot.send_video(
                user.id, _row_vm[0],
                caption="🎬 Видеоинструкция: подключение и первая тренировка с ботом",
                reply_markup=_add_main_menu_btn(None))
        else:
            await context.bot.send_message(
                user.id, "🎬 Видеоинструкция готовится — скоро появится здесь.",
                reply_markup=_add_main_menu_btn(None))

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

    elif query.data in ("analyze_test_interval", "analyze_test_long"):
        if user.id not in ADMIN_TELEGRAM_IDS:
            return
        is_long = query.data == "analyze_test_long"
        await query.edit_message_text(
            f"🧪 Ищу {'Long Run' if is_long else 'интервальную'} тренировку в канале..."
        )
        workout = await (find_next_long_run() if is_long else find_next_workout(only_interval=True))
        if not workout:
            await query.edit_message_text("😔 Не нашёл подходящую тренировку в канале.")
            return
        await _run_analyze_test(workout, query, context)

    elif query.data in ("reanalyze_interval", "reanalyze_long"):
        if user.id not in ADMIN_TELEGRAM_IDS:
            return
        is_long = query.data == "reanalyze_long"
        mode = get_preprocess_mode()
        await query.edit_message_text(
            f"💾 Ищу {'Long Run' if is_long else 'интервальную'} тренировку в канале..."
        )
        workout = await (find_next_long_run() if is_long else find_next_workout(only_interval=True))
        if not workout or not workout.get("post_id"):
            await query.edit_message_text("😔 Не нашёл подходящую тренировку в канале.")
            return
        await query.edit_message_text(
            f"💾 Боевой переразбор через DeepSeek (режим {mode})...\nМожет занять 1-2 минуты."
        )
        text = await _reanalyze_one(workout, mode, context)
        first = True
        for i in range(0, len(text), 4096):
            chunk = text[i:i + 4096]
            if first:
                await query.edit_message_text(chunk)
                first = False
            else:
                await context.bot.send_message(user.id, chunk)

    elif query.data.startswith("brief_gen_"):
        if user.id not in ADMIN_TELEGRAM_IDS:
            return
        import json as _json_b
        import announce_brief
        from database import get_workout_analysis
        try:
            _pid = int(query.data.replace("brief_gen_", ""))
        except ValueError:
            return
        rec = get_workout_analysis(_pid)
        if not rec:
            await query.edit_message_text("Анализ не найден.")
            return
        await query.edit_message_text("🧭 Считаю режимы (deep, 2-3 минуты)...")
        try:
            _res = _json_b.loads(rec.get("analyzed_json") or "{}")
        except Exception:
            await query.edit_message_text("Анализ не распарсился.")
            return
        brief = await asyncio.to_thread(announce_brief.build_admin_brief, _res, _pid, "deep")
        if not brief:
            await query.edit_message_text("Не удалось построить режимы.")
            return
        first = True
        for i in range(0, len(brief), 4096):
            chunk = brief[i:i + 4096]
            if first:
                await query.edit_message_text(chunk)
                first = False
            else:
                await context.bot.send_message(user.id, chunk)

    # ── ОБРАТНАЯ СВЯЗЬ ────────────────────────────────────────

    elif query.data == "feedback_show":
        await query.edit_message_text("Выбери тип:", reply_markup=_build_feedback_keyboard())

    elif query.data in ("feedback_bug", "feedback_feature"):
        fb_type = "bug" if query.data == "feedback_bug" else "feature"
        type_label = "проблему" if fb_type == "bug" else "идею"
        context.user_data["awaiting_feedback"] = fb_type
        await query.edit_message_text(f"Опиши {type_label}:")

    # ── ОЦЕНКА РЕКОМЕНДАЦИИ ───────────────────────────────────

    elif query.data == "howto_garmin":
        await query.answer()
        await context.bot.send_message(user.id, GARMIN_HOWTO)
        return

    elif query.data in ("pfb_faster", "pfb_ok", "pfb_slower"):
        ctx = _rating_data.get(user.id) or {}
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        answers = {"pfb_faster": "faster", "pfb_ok": "ok", "pfb_slower": "slower"}
        try:
            from database import save_pace_feedback
            save_pace_feedback(db_user_id, ctx.get("workout_date") or "",
                               answers[query.data],
                               rec_group=str(ctx.get("rec_group") or "") or None,
                               ai_mode=ctx.get("ai_mode"))
            try:
                _km = (query.message.reply_markup.inline_keyboard
                       if query.message and query.message.reply_markup else [])
                _rows = [r for r in _km
                         if not any((b.callback_data or "").startswith("pfb_") for b in r)]
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup(_rows))
            except Exception:
                pass
            await query.answer("Записал, спасибо! 🙌")
        except Exception as e:
            logger.error(f"pace_feedback: {e}")
            await query.answer("Не сохранилось, попробуй позже.")
        return

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
        save_vo2max_manual(db_user_id, vo2max)
        try:
            zones.recalculate_and_save(db_user_id)
        except Exception as e:
            logger.warning(f"Zones recalc error (manual vo2max) for {user.id}: {e}")
        context.user_data.pop("awaiting_profile")
        profile = get_user_profile(db_user_id)
        await update.message.reply_text(
            f"✅ VO2max сохранён: {vo2max} мл/кг/мин\n\n{_build_profile_text(profile, db_user_id)}",
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
        save_lt_manual(db_user_id, pace, None)
        try:
            zones.recalculate_and_save(db_user_id)
        except Exception as e:
            logger.warning(f"Zones recalc error (manual lactate) for {user.id}: {e}")
        context.user_data["awaiting_profile"] = "set_lactate_hr"
        context.user_data["lactate_pace"] = pace
        await update.message.reply_text(
            f"Темп {pace} сохранён.\n\nТеперь пульс на лактатном пороге — средний за такой бег "
            f"(обычно 85–90% от максимального).\n\nНапример: 174"
        )

    elif context.user_data.get("awaiting_profile") == "set_lactate_hr":
        import re
        if not re.match(r'^\d{2,3}$', text.strip()):
            await update.message.reply_text("Введи пульс числом, например: 174")
            return
        hr = int(text.strip())
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        save_user_profile(db_user_id, lactate_threshold_hr=hr, lactate_source="manual")
        save_lt_manual(db_user_id, None, hr)
        pace = context.user_data.pop("lactate_pace", "")
        context.user_data.pop("awaiting_profile")
        profile = get_user_profile(db_user_id)
        await update.message.reply_text(
            f"✅ Лактатный порог сохранён: {pace} мин/км при ЧСС {hr} уд/мин\n\n{_build_profile_text(profile, db_user_id)}",
            reply_markup=_build_profile_keyboard(profile)
        )

    # Email для Garmin
    elif context.user_data.get("awaiting_garmin") == "mfa":
        code = text.strip()
        pending = context.user_data.get("garmin_mfa") or {}
        if code.lower() in ("отмена", "cancel", "/cancel"):
            context.user_data.pop("awaiting_garmin", None)
            context.user_data.pop("garmin_mfa", None)
            await update.message.reply_text("Отменено. Подключить заново — /connect_garmin")
            return
        context.user_data.pop("awaiting_garmin", None)
        context.user_data.pop("garmin_mfa", None)
        try:
            await update.message.delete()
        except Exception:
            pass
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        msg = await update.effective_chat.send_message("⏳ Проверяю код...")
        try:
            from garmin import connect_finish, apply_profile_after_connect
            await connect_finish(db_user_id, pending["session"], code)
            save_user_profile(db_user_id,
                              garmin_email=pending.get("email", ""),
                              garmin_password=pending.get("password", ""))
            lines = ["✅ Garmin подключён!", ""] + await apply_profile_after_connect(db_user_id)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📖 Как получить разбор", callback_data="howto_garmin")],
                [InlineKeyboardButton("← Сервисы", callback_data="show_services")],
            ])
            await msg.edit_text("\n".join(lines), reply_markup=keyboard)
            n = count_users_with_service("garmin")
            uname = f" (@{user.username})" if user.username else ""
            await _notify_admin(
                context.bot,
                f"🔵 {user.full_name}{uname} подключил Garmin (двухфакторка)\n"
                f"Всего с Garmin: {n}")
        except Exception as e:
            logger.error(f"Garmin MFA error: {e}")
            await msg.edit_text(
                "❌ Код не подошёл или истёк срок его жизни.\n"
                "Начни заново: /connect_garmin")
        return
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
            from garmin import connect_start, connect_finish
            needs_mfa, session = await connect_start(email, password)
            if needs_mfa:
                context.user_data["awaiting_garmin"] = "mfa"
                context.user_data["garmin_mfa"] = {
                    "session": session, "email": email, "password": password}
                await msg.edit_text(
                    "🔐 В аккаунте включена двухфакторная защита.\n"
                    "Garmin прислал код на почту — пришли его следующим сообщением "
                    "(или напиши «отмена»).")
                return
            await connect_finish(db_user_id, session)
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
                save_vo2max_device(db_user_id, float(vo2max), "garmin")
            if garmin_lt_found:
                save_lt_device(db_user_id, lt["pace"], lt.get("hr"), "garmin")
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
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("📖 Как получить разбор", callback_data="howto_garmin")],
                    [InlineKeyboardButton("← Сервисы", callback_data="show_services")],
                ])
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
                save_vo2max_device(db_user_id, float(vo2max), "coros")
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

    # ── /msg_service: текст всем с подключённым сервисом (admin) ──
    elif context.user_data.get("awaiting_msg_service"):
        svc = context.user_data.pop("awaiting_msg_service")
        if text.strip().lower() in ("отмена", "cancel", "/cancel"):
            await update.message.reply_text("Отменено, ничего не отправлено.")
            return
        if _audience_name(svc):
            from database import get_users_by_audience
            targets = get_users_by_audience(svc)
        else:
            targets = get_users_with_service_full(svc)
        sent, failed = 0, []
        for tg_id, name, uname in targets:
            try:
                await context.bot.send_message(tg_id, text)
                sent += 1
            except Forbidden:
                await _report_block(context.bot, tg_id, "msg_service")
                failed.append(f"{name or tg_id}: заблокировал бота")
            except Exception as e:
                failed.append(f"{name or tg_id}: {type(e).__name__}")
            await asyncio.sleep(0.05)
        _lbl = _audience_name(svc) or _svc_name(svc)
        report = f"✅ {_lbl}: отправлено {sent} из {len(targets)}"
        if failed:
            report += "\n❌ Не дошло:\n" + "\n".join(failed[:20])
        await update.message.reply_text(report)
        return
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
    force_mode: str | None = None,
) -> None:
    """Вариант B: ИИ сам выбирает группу.
    Для deep/fast/smart — основной путь рекомендации (вместо A).
    Для /b и /test_workout — вызывается через create_task (админ).
    """
    import functools
    db_user_id = user_data.get("db_user_id")
    rec_mode = force_mode or (get_preferences(db_user_id) or {}).get("ai_mode", "smart")

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
        advice["low_recovery"] = bool(scenario_ctx.get("low_recovery"))
        msg_text = claude_advisor.format_evening_message(
            advice | {"athlete_line": _athlete_line(db_user_id)}, workout_for_render, stats, weather_line=weather_line
        )
        msg_text = scenario_ctx["user_text"] + "\n\n" + msg_text
        _rating_data[telegram_id] = {
            "workout_date": analysis.get("workout_date", ""),
            "ai_mode": rec_mode,
            "rec_group": advice.get("recommended_group"),
        }
        rating_markup = InlineKeyboardMarkup([
            _pace_feedback_row(),
            [InlineKeyboardButton("⭐ Оценить рекомендацию", callback_data="rate_show"),
             InlineKeyboardButton("📖 Как получить разбор", callback_data="howto_garmin")],
        ])
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
    force_mode: str | None = None,
    target_date: str | None = None,
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
        live = await (find_next_long_run() if long else find_next_workout(target_date=target_date))
    cur_post = live.get("post_id") if live else None
    cur_date = live.get("workout_date") if live else None
    cur_edit = live.get("edit_date") if live else None

    row, status = get_latest_workout_analysis(wtype, cur_post, cur_date, cur_edit,
                                              target_date=target_date)

    async def _out(text, markup=None, parse_mode=None):
        # 19.08.2026: в лонг-карточке остаются ссылки из анонса (регистрация, камера
        # хранения) — глушим только превью сайта, сами ссылки кликабельны.
        _no_prev = bool(long)
        if msg:
            await msg.edit_text(text, reply_markup=markup, parse_mode=parse_mode,
                                disable_web_page_preview=_no_prev)
        else:
            await context.bot.send_message(telegram_id, text, reply_markup=markup,
                                           parse_mode=parse_mode,
                                           disable_web_page_preview=_no_prev)

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
        # Рассылка читает КЭШ (прогрет в 19:00 джобом scheduled_recovery_prefetch),
        # живые походы в трекеры — только ручные команды (/workout и т.п.).
        "recovery": await _get_unified_recovery(
            db_user_id, force_fresh=(not _is_past_rt) and (not is_broadcast)),
    }

    rec_mode = force_mode or (get_preferences(db_user_id) or {}).get("ai_mode", "smart")
    if is_broadcast and not force_mode:
        # Рассылка использует 2 режима из 4 (решение 14.08.2026): smart→deep
        # (глубокий живее и экономнее умного: 144с/11.5k против 164с/19k),
        # calc→fast (формульным — полноценная лёгкая ИИ-карточка за секунды).
        rec_mode = {"smart": "deep", "calc": "fast"}.get(rec_mode, rec_mode)
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
                                 msg=msg, is_broadcast=is_broadcast, force_mode=force_mode)
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
        "rec_group": (rec or {}).get("recommended_group"),
    }
    rating_markup = InlineKeyboardMarkup([
        _pace_feedback_row(),
        [InlineKeyboardButton("⭐ Оценить рекомендацию", callback_data="rate_show")]
        + ([] if long else
           [InlineKeyboardButton("📖 Как получить разбор", callback_data="howto_garmin")]),
    ])
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
    rec_mode = force_mode or (get_preferences(db_user_id) or {}).get("ai_mode", "smart")
    if is_broadcast and not force_mode:
        # Тот же маппинг 2-из-4, что и выше — для calc/long-пути
        rec_mode = {"smart": "deep", "calc": "fast"}.get(rec_mode, rec_mode)
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
            advice | {"athlete_line": _athlete_line(db_user_id)}, workout_dict,
            stats=stats2, weather_line=weather_line, has_tracker=has_tracker)

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
        if profile.get("vo2max"):
            fitness["vo2max"] = profile["vo2max"]
            fitness["vo2max_source"] = profile.get("vo2max_source") or "профиль"
            fitness["vo2max_resolved"] = True
        if profile.get("lactate_threshold_pace"):
            # В промт — применяемый порог (из зон), а не сырой ЛП из профиля
            fitness["lactate_threshold_pace"] = (_applied_threshold(db_user_id)
                                                or profile["lactate_threshold_pace"])
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
    text = format_evening_message(advice | {"athlete_line": _athlete_line(db_user_id)}, workout, stats=stats, weather_line=weather_line, profile_only=_profile_only, has_tracker=has_tracker)

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
            'rec_group': rec_group,
        }
        fit_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("⌚ Загрузить в Garmin", callback_data="fit_up"),
        ]])
        rating_markup = InlineKeyboardMarkup([
            _pace_feedback_row(),
            [InlineKeyboardButton("⭐ Оценить рекомендацию", callback_data="rate_show")],
        ])

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
        if profile.get("vo2max"):
            fitness["vo2max"] = profile["vo2max"]
            fitness["vo2max_source"] = profile.get("vo2max_source") or "профиль"
            fitness["vo2max_resolved"] = True
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
            'rec_group': 'лонг',
        }
        fit_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("⌚ Загрузить в Garmin", callback_data="fit_up"),
        ]])
        rating_markup = InlineKeyboardMarkup([
            _pace_feedback_row(),
            [InlineKeyboardButton("⭐ Оценить рекомендацию", callback_data="rate_show")],
        ])

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
    with_ceilings: bool = True,
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
    # 06.09.2026: флаг низкого восстановления по единой шкале — для жёлтой покраски рекомендации в карточке
    scenario_ctx["low_recovery"] = claude_advisor.recovery_is_low(recovery)
    # Максимально повторяемый темп по скоростным блокам (см. PROCESS_MAP.md):
    # отсекает группы, чей финишный темп быстрее повторяемого для этой длины.
    _repeat_caps = []
    _rep_pace = zones_map.get("repetition")
    _rtype = (zinfo or {}).get("runner_type")
    if _rep_pace:
        _seen_dist: set[int] = set()
        for _b in (analysis.get("structure") or []):
            if _b.get("type") != "repeat":
                continue
            _d = _b.get("work_distance_m") or 0
            if 50 <= _d <= 400 and _d not in _seen_dist:
                _seen_dist.add(_d)
                _p = _z.repeat_pace_for_distance(_rep_pace, _d, _rtype)
                if _p:
                    _repeat_caps.append({"distance_m": _d, "ceiling": _p})

    prompt = claude_advisor.build_ai_b_prompt(
        analysis, user_data, zones_map, recovery,
        recovery_scenario_text=scenario_ctx["prompt_text"],
        speed_ceilings=(_repeat_caps or None) if with_ceilings else None,
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
            await _report_block(context.bot, telegram_id, "рассылка сообщений")
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
            await _report_block(context.bot, telegram_id, "рассылка сообщений")
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


# ── Единое ядро разбора анонса (Шаг 1) ──────────────────────────
# Один разбор для всех путей: шедулер, боевой /reanalyze, тестовый прогон.
# Ядро только считает и ничего не пишет; запись — отдельной функцией сверху.

async def _analyze_core(workout: dict, mode: str) -> dict | None:
    """Разбор анонса моделью + предохранители. В БД НЕ пишет.
    Возвращает result-dict (готов к сохранению) или None, если модель не ответила.
    """
    import json as _json, functools, re as _re
    import claude_advisor as _ca_strip
    result = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(analyze_workout,
                                workout.get("raw_text") or "",
                                workout.get("comments_text") or "", mode)
    )
    if not result:
        return None
    # Предохранитель: анонс без групп С ТЕМПАМИ бесполезен для рекомендации
    # (прогрев-посты с одной группой «здоровье» без темпа — это не анонс).
    if result.get("is_valid"):
        paced = [g for g in (result.get("groups") or [])
                 if _re.search(r"\d{1,2}:\d{2}", _json.dumps(g, ensure_ascii=False))]
        if not paced:
            result["is_valid"] = False
            result["reject_reason"] = "нет групп с темпами — не анонс"
            logger.info(f"анализ: post_id={workout.get('post_id')} is_valid сброшен "
                        f"(нет групп с темпами)")
    result["work_text"] = _ca_strip.strip_links(workout.get("work_text"))
    return result


def _store_analysis(result: dict, workout: dict, mode: str) -> None:
    """ЕДИНСТВЕННАЯ точка записи разбора: анализ + эталоны за ту же дату.
    Формат данных один для всех боевых путей (шедулер, /reanalyze).
    """
    import json as _json
    wdate = result.get("workout_date", "")
    save_workout_analysis(
        post_id=workout.get("post_id"),
        workout_date=wdate,
        workout_type=result.get("workout_type", ""),
        is_valid=1 if result.get("is_valid") else 0,
        raw_text=workout.get("raw_text") or "",
        analyzed_json=_json.dumps(result, ensure_ascii=False),
        analysis_mode=mode,
        extra_groups_json=_json.dumps(workout.get("extra_groups") or [], ensure_ascii=False),
        edit_date=workout.get("edit_date"),
    )
    # Эталоны по группам — из этого же разбора (лонг repeat-блоков не имеет).
    if result.get("is_valid") and result.get("workout_type") != "long":
        try:
            _save_workout_templates(result, wdate)
        except Exception as e:
            logger.error(f"эталоны при сохранении разбора: {e}")


async def _autoanalyze_post(workout: dict, context=None) -> None:
    """Фоновый автоанализ анонса (Шаг 1) → запись в workout_analysis.
    Запускается при: новом анонсе / новой доп. группе / редактировании поста.
    Прод-режим (get_preprocess_mode). Не блокирует цикл проверки.
    После успешного анализа уведомляет ТОЛЬКО админа (контроль, что бот поймал анонс).
    Пользователям ничего не шлёт — их единственное сообщение это вечерняя рассылка 20:00.
    """
    import json as _json
    try:
        post_id = workout.get("post_id")
        if not post_id:
            return
        raw_text = workout.get("raw_text") or ""
        edit_date = workout.get("edit_date")
        extra = workout.get("extra_groups") or []

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
        result = await _analyze_core(workout, mode)
        if not result:
            logger.warning(f"autoanalyze: post_id={post_id} анализ не удался ({reason})")
            return
        _store_analysis(result, workout, mode)
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
            # Бриф режимов (Шаг 1.5) — только при ПЕРВОМ анализе анонса (доп. группы/правки
            # не дублируют). Изолированно: ошибка не роняет автоанализ.
            # 20.08.2026: режимы пересобираем при ЛЮБОМ переанализе — иначе после новых
            # доп. групп analyzed_json остаётся без modes и вечерний бриф молча не
            # публикуется. Текст админу — только при первом анализе, чтобы не спамить.
            try:
                import announce_brief
                brief = await asyncio.to_thread(
                    announce_brief.build_admin_brief, result, post_id, "deep")
                if brief and reason == "новый анонс":
                    for i in range(0, len(brief), 4096):
                        await _notify_admin(context.bot, brief[i:i + 4096])
            except Exception as e:
                logger.warning(f"autoanalyze: announce_brief error: {e}")
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
    if get_token(db_user_id, "coros_mcp"):
        import coros_mcp as _cm
        await _cm.fetch_raw(db_user_id)
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


async def scheduled_recovery_prefetch(context: ContextTypes.DEFAULT_TYPE):
    """19:00 МСК — прогрев данных к вечерней рассылке (решение 14.08.2026).
    Мини-версия wakeup_poll без логики поимки: fetch_raw ночных сервисов
    (garmin/coros/polar/whoop; Strava СОБЫТИЙНАЯ — не опрашивается) +
    нормализация → unified_cache свеж к 20:00. Рассылка читает кэш
    (force_fresh=False при is_broadcast) — ноль живых походов между юзерами,
    битые креды виснут здесь, в тихий час. Живые данные по запросу — только /workout."""
    users = get_all_users_with_status()
    n = fails = 0
    for telegram_id, name, _un, _has in users:
        db_user_id = get_or_create_user(telegram_id, name)
        if not _night_services(db_user_id):
            continue
        try:
            await _sync_night_services(db_user_id)
            _normalize_after_catch(db_user_id)
            n += 1
        except Exception as e:
            fails += 1
            logger.warning(f"recovery prefetch error for {telegram_id}: {e}")
        await asyncio.sleep(1)
    logger.info(f"Прогрев восстановления к рассылке: обновлено {n}, ошибок {fails}")


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

def _save_workout_templates(parsed: dict | None, workout_date: str | None) -> None:
    """Сохраняет эталоны (готовый Garmin JSON) по всем группам из разбора Шага 1.
    На вход — УЖЕ РАЗОБРАННЫЙ анализ (dict) и дата тренировки.
    Только интервальные. Не критично — сбой не должен ронять рассылку/разбор.
    Группа «здоровье» пропускается: у неё нет темпов, эталон бессмысленен.
    """
    import json as _json
    from fit_generator import build_garmin_from_analysis
    if not parsed or not workout_date:
        return
    saved = 0
    for g in (parsed.get("groups") or []):
        gnum = str(g.get("number") or "").strip()
        if not gnum or "здоров" in gnum.lower():
            continue
        try:
            wj = build_garmin_from_analysis(parsed, gnum)
            save_workout_template(workout_date, gnum, "interval",
                                  _json.dumps(wj, ensure_ascii=False))
            saved += 1
        except Exception as e:
            logger.error(f"эталон группы {gnum} не сохранён: {e}")
    logger.info(f"эталоны тренировки: {saved} групп на {workout_date}")


async def cmd_resend_evening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Досылка вечерней рекомендации ТОЛЬКО недополучившим (без дублей):
    активные подписанные без last_recommendation на дату. Создана после обрыва
    рассылки 13.08 (рестарт убил процесс на середине).
    /resend_evening 20260814 [fast|smart|deep] — режим принудительно для всех
    досылаемых (по умолчанию fast — быстро догнать при медленном DeepSeek)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    from database import get_connection
    args = context.args or []
    digits = "".join(ch for ch in (args[0] if args else "") if ch.isdigit())
    if len(digits) != 8:
        await update.message.reply_text("Формат: /resend_evening 20260814 [fast|smart|deep]")
        return
    wdate = f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    fmode = args[1] if len(args) > 1 and args[1] in ("fast", "smart", "deep") else "fast"
    with get_connection() as conn:
        targets = conn.execute("""
            SELECT u.telegram_id, COALESCE(u.name, u.username, 'user')
            FROM users u
            LEFT JOIN user_preferences p ON u.id = p.user_id
            WHERE (p.is_active IS NULL OR p.is_active = 1)
              AND (p.notify_interval IS NULL OR p.notify_interval = 1)
              AND u.telegram_id > 0 AND u.telegram_id < 900000000
              AND u.id NOT IN (SELECT user_id FROM last_recommendation
                               WHERE workout_date = ?)
        """, (wdate,)).fetchall()
    await update.message.reply_text(
        f"Досылаю {len(targets)} недополучившим ({wdate}, режим {fmode})...")
    ok = fail = 0
    for tid, name in targets:
        try:
            await _send_recommendation(tid, name, context, long=False,
                                       is_broadcast=True, force_mode=fmode)
            ok += 1
        except Forbidden:
            _mark_user_inactive(tid)
            fail += 1
        except Exception as e:
            logger.warning(f"resend to {tid}: {e}")
            fail += 1
        await asyncio.sleep(0.5)
    await update.message.reply_text(f"Досылка завершена: отправлено {ok}, ошибок {fail}")


async def _send_notify_hint(bot, telegram_id: int) -> None:
    """20.08.2026: после каждого АВТОМАТИЧЕСКОГО сообщения — отдельная мини-плашка
    с кнопкой на настройку рассылки: отключение должно быть на виду, а не в меню.
    Не критична: ошибка плашки не должна ломать саму рассылку."""
    try:
        await bot.send_message(
            telegram_id,
            "⚙️ Управление рассылкой — выбери, какие сообщения получать:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🔔 Настройка уведомлений", callback_data="notifications")]]))
    except Exception as e:
        logger.warning(f"notify hint for {telegram_id}: {e}")


async def scheduled_brief_comment(context: ContextTypes.DEFAULT_TYPE) -> None:
    """20.08.2026: бриф-комментарий под анонсом — ОТДЕЛЬНЫЙ джоб в 19:05 МСК,
    а не внутри рассылки: в 20:00 бот занят рассылкой, а на бриф идут новые люди.
    Только интервальные анонсы (в сб лонг — пропускаем, как было раньше)."""
    now = datetime.now()
    # 21.08.2026: бриф — только пн и чт (как вечерняя рассылка накануне вт/пт).
    # Раньше джоб был ежедневным с исключением субботы — и в пятницу повторно
    # публиковал бриф под тем же анонсом.
    if now.weekday() not in (0, 3):
        return
    live = await find_next_workout()
    cur_post = live.get("post_id") if live else None
    if not cur_post:
        logger.info("Бриф-комментарий пропущен: нет актуального анонса")
        return
    try:
        from database import get_workout_analysis as _gwa
        import json as _json_bc
        _brec = _gwa(cur_post)
        _bresult = _json_bc.loads(_brec.get("analyzed_json") or "{}") if _brec else None
        _modes = (_bresult or {}).get("modes")
        if _modes and _bresult:
            import announce_brief as _ab
            import telegram_reader as _tr
            _btext = _ab.format_brief(_bresult, _modes)
            _btext += ("\n\n🤖 Персональная группа под твою форму, тренировка в часы "
                       "и разбор после финиша — @DD_adviser_bot\n"
                       "🌐 О сервисе — dodick.run")
            if await _tr.post_comment(cur_post, _btext):
                logger.info(f"Бриф опубликован комментарием к посту {cur_post}")
            else:
                logger.warning(f"Бриф-комментарий к посту {cur_post} не опубликован")
        else:
            logger.info("Бриф-комментарий пропущен: нет режимов в анализе")
    except Exception as e:
        logger.error(f"Бриф-комментарий: {e}")


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

    # Бриф комментарием под анонсом (решение 11.08.2026): перед персональной
    # рассылкой публикуем режимы в ветку обсуждения анонса (от аккаунта Антона
    # через Telethon — бота в чат не добавить). Только интервалы (у лонгов нет
    # режимов). Любая ошибка — только лог, рассылка не страдает.
    # 20.08.2026: бриф-комментарий переехал в отдельный джоб scheduled_brief_comment (19:05 МСК)

    users = get_all_users_with_status()
    count = 0
    sent: list[tuple] = []  # (telegram_id, name, username) — для блока «без рекомендации» в отчёте
    # Параллельная рассылка (14.08.2026): семафор на 5 одновременных — безопасно,
    # потому что живых фетчей в broadcast-пути больше нет (кэш + прогрев 19:00),
    # параллелятся только вызовы DeepSeek и отправки Telegram.
    _sem = asyncio.Semaphore(5)

    async def _mail_one(telegram_id, name, _un):
        nonlocal count
        async with _sem:
            try:
                await _send_recommendation(telegram_id, name, context, long=is_long, live=live, is_broadcast=True)
                count += 1
                sent.append((telegram_id, name, _un))
                await _send_notify_hint(context.bot, telegram_id)
                await asyncio.sleep(0.5)
            except Forbidden:
                await _report_block(context.bot, telegram_id, "вечерняя рассылка")
            except Exception as e:
                logger.error(f"Evening notification error for {telegram_id}: {e}")

    await asyncio.gather(*[_mail_one(t, n, u) for t, n, u, _has in users])
    logger.info(f"Вечерняя рассылка завершена ({wtype}, status={status}): {count} отправлено (кэш, параллель ×5)")

    # Эталоны теперь пишутся вместе с разбором (_store_analysis) — здесь страховка
    # на случай анализов, сохранённых до перехода на единое ядро.
    if not is_long:
        try:
            import json as _json_t
            _parsed_t = _json_t.loads((analysis or {}).get("analyzed_json") or "{}")
            _save_workout_templates(_parsed_t, live.get("workout_date") if live else None)
        except Exception as e:
            logger.error(f"эталоны (рассылка): {e}")

    base = (f"📨 Рассылка завершена\n"
            f"Тип: {wtype} | Отправлено: {count} пользователям")

    # Отчёт по разосланным рекомендациям — НЕ критичен, не должен ронять уведомление.
    report = ""
    try:
        bcast_date = live.get("workout_date") if live else None
        report = _build_mailing_report(bcast_date, sent) if bcast_date else ""
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

    # Проверка протухших кредов — ЗДЕСЬ, а не в ночном джобе: сообщения пользователям
    # уходят вечером вместе с рекомендацией, а не в три ночи (решение 09.08.2026).
    try:
        await _check_stale_credentials(context)
    except Exception as e:
        logger.error(f"stale credentials check error: {e}")


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
        # Убрана из расписания 08.08.2026: данные приходят по вебхуку activity.create
        # (oauth_server._strava_activity_ingest: fetch_raw → нормализация →
        # refresh_athlete_cache). Страховки: /refresh, OAuth-подключение.

        # Ночной забор сырья (Garmin/COROS/Polar/Whoop) + нормализация —
        # перенесены в scheduled_wakeup_poll (по факту поимки ночи). Здесь не дублируем.

        # ── VO2max из трекера (тихо) ───────────────────────────
        new_vo2max, tracker_key, tracker_name = await _get_vo2max_from_tracker(db_user_id)
        if new_vo2max is not None:
            save_vo2max_device(db_user_id, float(new_vo2max), tracker_key or "auto")
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

        # Strava — убрана из расписания 08.08.2026 (вебхук activity.create,
        # см. oauth_server._strava_activity_ingest)

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
                        save_vo2max_device(db_user_id, float(vo2max), "garmin")
                        if not (profile or {}).get("vo2max_locked"):
                            save_user_profile(db_user_id, vo2max=vo2max, vo2max_source="garmin")
                            vo2max_ok += 1
                    if not isinstance(lt, Exception) and lt:
                        save_lt_device(db_user_id, lt["pace"], lt.get("hr"), "garmin")
                        if not (profile or {}).get("lactate_locked"):
                            save_user_profile(db_user_id,
                                              lactate_threshold_pace=lt["pace"],
                                              lactate_threshold_hr=lt["hr"],
                                              lactate_source="auto")
            except Exception as e:
                logger.error(f"Garmin VO2max refresh error for {telegram_id}: {e}")

        await asyncio.sleep(1)

    logger.info(f"Обновление завершено: Strava={strava_ok}, Garmin recovery={garmin_ok}, VO2max={vo2max_ok}")


async def _check_stale_credentials(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ищет активных пользователей, у кого креды сервиса есть, а сырьё старше 72 часов —
    признак сменённого пароля (COROS отвечает 1019, re-auth бесполезен).
    Пользователю — сообщение с кнопками «обновить пароль / отключить» (не чаще раза
    в 7 дней, маркер в bot_settings), админу — сводка каждый день.
    Сервисы с паролями: garmin, coros (OAuth-сервисы обновляют токены сами)."""
    from database import get_connection
    from datetime import date as _date
    today = _date.today().isoformat()
    creds_col = {"garmin": "garmin_email", "coros": "coros_email"}
    admin_lines: list[str] = []
    with get_connection() as conn:
        for svc, col in creds_col.items():
            rows = conn.execute(f"""
                SELECT u.id, u.telegram_id, COALESCE(u.username, u.name),
                       r.fetched_at
                FROM users u
                JOIN user_profile p ON p.user_id = u.id AND p.{col} IS NOT NULL
                LEFT JOIN user_preferences pref ON pref.user_id = u.id
                LEFT JOIN raw_service_data r ON r.user_id = u.id AND r.service = ?
                WHERE (pref.is_active IS NULL OR pref.is_active = 1)
                  AND (r.fetched_at IS NULL
                       OR r.fetched_at < datetime('now', '-72 hours'))
            """, (svc,)).fetchall()
            for uid, tid, uname, fat in rows:
                admin_lines.append(
                    f"  {_svc_name(svc)}: @{uname or uid} — сырьё от {fat or 'никогда'}")
                key = f"stale_notice_{uid}_{svc}"
                last = conn.execute(
                    "SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
                if last and (_date.fromisoformat(today)
                             - _date.fromisoformat(last[0])).days < 7:
                    continue
                connect_cb = next(
                    (cb for s, _, _, cb, _ in _SERVICES if s == svc), None)
                try:
                    await context.bot.send_message(
                        tid,
                        f"⚠️ Данные {_svc_name(svc)} не обновляются с "
                        f"{(fat or 'момента подключения')[:10]}.\n"
                        f"Похоже, пароль изменился или доступ пропал. Обнови пароль в боте — "
                        f"или отключи сервис, если больше им не пользуешься.",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔑 Обновить пароль",
                                                 callback_data=connect_cb),
                            InlineKeyboardButton("🔌 Отключить",
                                                 callback_data=f"disc_ask_{svc}"),
                        ]]))
                    conn.execute(
                        "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
                        (key, today))
                except Forbidden:
                    _mark_user_inactive(tid)
                except Exception as e:
                    logger.warning(f"stale notice to {tid} failed: {e}")
    if admin_lines:
        logger.info(f"stale check: {len(admin_lines)} протухших подключений, сводка админу")
        await _notify_admin(context.bot,
                            "🔒 Протухшие подключения (креды есть, сырья нет >72ч):\n"
                            + "\n".join(admin_lines))


async def scheduled_morning(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    # вт/пт — 07:00 МСК. Воскресенье вынесено в scheduled_morning_sunday (07:30 МСК).
    if now.weekday() not in [1, 4]:
        return
    logger.info("Запускаю утреннюю рассылку...")
    # Без профиля/трекера не беспокоим; 17.08.2026 — фильтр по галочке утренних
    users = [(tid, name, un) for tid, name, un, has
             in get_all_users_with_status("notify_morning_interval") if has]
    for telegram_id, name, _ in users:
        try:
            await _send_morning_check(telegram_id, context)
            await _send_notify_hint(context.bot, telegram_id)
            await asyncio.sleep(0.5)
        except Forbidden:
            await _report_block(context.bot, telegram_id, "утренняя рассылка")
        except Exception as e:
            logger.error(f"Morning notification error for {telegram_id}: {e}")


async def scheduled_cache_refresh_sunday(context: ContextTypes.DEFAULT_TYPE):
    """04:15 UTC (07:15 МСК), только вс — позже будничного рефреша,
    чтобы к 07:15 Garmin успел обработать ночной сон. То же тело, что scheduled_cache_refresh."""
    await scheduled_cache_refresh(context)


async def scheduled_morning_sunday(context: ContextTypes.DEFAULT_TYPE):
    """04:30 UTC (07:30 МСК), только вс — после воскресного рефреша."""
    logger.info("Запускаю утреннюю рассылку (вс, 07:30 МСК)...")
    users = [(tid, name, un) for tid, name, un, has
             in get_all_users_with_status("notify_morning_long") if has]
    for telegram_id, name, _ in users:
        try:
            await _send_morning_check(telegram_id, context)
            await _send_notify_hint(context.bot, telegram_id)
            await asyncio.sleep(0.5)
        except Forbidden:
            await _report_block(context.bot, telegram_id, "воскресная рассылка")
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


async def w_user_light_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/w_user_light (admin) — рекомендация по пользователю принудительно в лёгком режиме (fast)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    users = get_users_list_for_b()
    if not users:
        await update.message.reply_text("Нет пользователей в базе.")
        return
    keyboard = [
        [InlineKeyboardButton(
            u["name"] + (f" (@{u['username']})" if u.get("username") else ""),
            callback_data=f"wul_{u['db_user_id']}"
        )]
        for u in users
    ]
    await update.message.reply_text(
        "⚡ Лёгкий режим (fast) — выбери пользователя:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def w_user_light_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбор юзера в /w_user_light — запуск с force_mode='fast'."""
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
    msg = await query.edit_message_text(
        f"⚡ Лёгкий режим — <b>{user['name']}</b>\n"
        f"Его режим: {user_mode} → считаю как fast (B)\nЗапускаю...",
        parse_mode="HTML",
    )
    await _send_recommendation(user["telegram_id"], user["name"], context,
                               long=False, msg=msg, force_mode="fast")


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


async def _build_analysis_and_user_data(db_user_id: int, target_date: str | None = None):
    """Возвращает (analysis, user_data, workout_dict, weather_line) или (None, ...) при ошибке."""
    import json as _json
    live = await find_next_workout(target_date=target_date)
    cur_post = live.get("post_id") if live else None
    cur_date = live.get("workout_date") if live else None
    cur_edit = live.get("edit_date") if live else None
    row, status = get_latest_workout_analysis("interval", cur_post, cur_date, cur_edit,
                                              target_date=target_date)
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

    arg = context.args[0] if context.args else ""
    target_date = _parse_cmd_date(arg)
    if arg and not target_date:
        await update.message.reply_text("Не понял дату. Формат: /p_b 20260905 или /p_b 0905")
        return

    analysis, user_data, workout_dict, _ = await _build_analysis_and_user_data(db_user_id, target_date)
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

    arg = context.args[0] if context.args else ""
    target_date = _parse_cmd_date(arg)
    if arg and not target_date:
        await update.message.reply_text("Не понял дату. Формат: /p_b_user 20260905 или /p_b_user 0905")
        return
    date_suffix = f"_{target_date.replace('-', '')}" if target_date else ""

    keyboard = [
        [InlineKeyboardButton(
            u["name"] + (f" (@{u['username']})" if u.get("username") else ""),
            callback_data=f"pb_user_{u['db_user_id']}{date_suffix}"
        )]
        for u in users
    ]
    await update.message.reply_text(
        f"📋 Промпт B{f' на {target_date}' if target_date else ''} — выбери пользователя:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def pb_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id not in ADMIN_TELEGRAM_IDS:
        await query.answer("Нет доступа.")
        return
    await query.answer()
    parts = query.data.split("_")            # pb_user_<id> или pb_user_<id>_<ГГГГММДД>
    db_user_id = int(parts[2])
    target_date = _parse_cmd_date(parts[3]) if len(parts) > 3 else None
    profile = get_user_profile(db_user_id) or {}
    user_name = profile.get("name") or profile.get("username") or f"user_{db_user_id}"
    analysis, user_data, workout_dict, _ = await _build_analysis_and_user_data(db_user_id, target_date)
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
    "get_report":   "/report",
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
    rep_cnt, rep_uniq = get_activity_report(days)
    lines += ["", f"📊 /report: {rep_cnt} (юзеров: {rep_uniq})"]
    for name, username, cnt, last_d in get_report_users(days):
        nick = f" (@{username})" if username else ""
        lines.append(f"  {name}{nick}: {cnt} · посл. {last_d}")
    who = get_activity_users(days)
    if who:
        lines += ["", f"Кто активен ({len(who)}, по дате последней активности):"]
        for name, username, cnt, last_d in who:
            nick = f" (@{username})" if username else ""
            lines.append(f"  {name}{nick}: {cnt} действий, посл. {last_d}")
    lines += ["", "Кнопки Тренировка/Long Run/Утро считаются как /workout, /long, /morning."]
    # 31.08: отчёт перерос лимит Telegram (4096) — шлём частями по строкам
    _chunk, _sent = [], 0
    for _ln in lines:
        if sum(len(x) + 1 for x in _chunk) + len(_ln) + 1 > 3800:
            await update.message.reply_text("\n".join(_chunk))
            _sent += 1
            _chunk = []
        _chunk.append(_ln)
    if _chunk:
        await update.message.reply_text("\n".join(_chunk), reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu_new")]]))


async def cmd_profile_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/profile_user (admin) — показать профиль выбранного пользователя (только текст)."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    users = get_users_list_for_b()
    if not users:
        await update.message.reply_text("Нет пользователей.")
        return
    keyboard = [
        [InlineKeyboardButton(
            u["name"] + (f" (@{u['username']})" if u.get("username") else ""),
            callback_data=f"profile_user_{u['db_user_id']}"
        )]
        for u in users
    ]
    await update.message.reply_text(
        "Выбери пользователя — покажу его профиль:",
        reply_markup=InlineKeyboardMarkup(keyboard))


async def profile_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатие на юзера в списке /profile_user — вывод его профиля."""
    query = update.callback_query
    if query.from_user.id not in ADMIN_TELEGRAM_IDS:
        await query.answer("Нет доступа.")
        return
    await query.answer()
    db_uid = int(query.data.rsplit("_", 1)[-1])
    users = get_users_list_for_b()
    target = next((u for u in users if u["db_user_id"] == db_uid), None)
    if not target:
        await query.edit_message_text("Пользователь не найден.")
        return
    from database import get_user_profile
    profile = get_user_profile(db_uid)
    uname = f" (@{target['username']})" if target.get("username") else ""
    text = _build_profile_text(profile, db_uid).replace("Твой профиль:", f"Профиль: {target['name']}{uname}", 1)
    text = text.replace("Твои скорости:", "Скорости:", 1)
    await query.edit_message_text(text)


# Кому шлём массовые сообщения кроме сервисов: ключ database.AUDIENCES — эмодзи — название.
_MSG_AUDIENCES = [
    ("active", "📣", "Все активные"),
    ("tracker", "⌚", "С трекером"),
    ("profileonly", "📝", "Только профиль (без трекера)"),
    ("emptyusers", "👻", "Без профиля и трекера"),
]


def _audience_name(kind: str) -> str | None:
    """Название вида получателей для подписей; None — если это сервис, а не вид."""
    return next((n.lower() for k, _, n in _MSG_AUDIENCES if k == kind), None)


async def cmd_msg_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/msg_service (admin) — рассылка текста всем, у кого подключён выбранный сервис.
    Показывает кнопки по _SERVICES с числом получателей; текст ждём следующим сообщением."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    rows = []
    for svc, emoji, name, _, _ in _SERVICES:
        n = len(get_users_with_service_full(svc))
        rows.append([InlineKeyboardButton(f"{emoji} {name} — {n}",
                                          callback_data=f"msgsvc_{svc}")])
    from database import get_users_by_audience
    for kind, emoji, name in _MSG_AUDIENCES:
        rows.append([InlineKeyboardButton(
            f"{emoji} {name} — {len(get_users_by_audience(kind))}",
            callback_data=f"msgsvc_{kind}")])
    await update.message.reply_text(
        "✉️ Кому отправить? Выбери сервис — получат все, у кого он подключён.",
        reply_markup=InlineKeyboardMarkup(rows))


async def msg_service_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбор сервиса в /msg_service — ждём текст следующим сообщением."""
    query = update.callback_query
    if query.from_user.id not in ADMIN_TELEGRAM_IDS:
        await query.answer("Нет доступа.")
        return
    await query.answer()
    svc = query.data.rsplit("_", 1)[-1]
    aud = _audience_name(svc)
    if aud:
        from database import get_users_by_audience
        n = len(get_users_by_audience(svc))
        lbl = f"«{aud}»"
    else:
        n = len(get_users_with_service_full(svc))
        lbl = _svc_name(svc)
    context.user_data["awaiting_msg_service"] = svc
    await query.edit_message_text(
        f"✉️ Напиши текст для пользователей {lbl} ({n} чел.) "
        f"следующим сообщением (или напиши «отмена»).")


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
        users = get_users_list_for_b(all_users=True)
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
    users = get_users_list_for_b(all_users=True)
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


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     target_db_user_id: int | None = None,
                     selector_override: str | None = None) -> None:
    """/report — ИИ-анализ тренировки: собирает пакет данных (профиль,
    план, факт по отрезкам, утренний снимок) и шлёт в ИИ, возвращает разбор тренера.
    Read-only, рабочие ветки не трогает.
    /report — последняя DD; /report DD_20260612 | /report 23219097987 — выбор тренировки;
    /report simple|s [селектор] — только графики, без вызова ИИ;
    /report data [селектор] — сырой пакет данных + промпт (без вызова ИИ)."""
    chat_id = update.effective_user.id
    db_user_id = target_db_user_id or get_or_create_user(update.effective_user.id, update.effective_user.full_name)
    # /report доступен любому, у кого подключён Garmin или Strava (источник разбора).
    if not (get_token(db_user_id, "garmin") or get_token(db_user_id, "strava")):
        await context.bot.send_message(
            chat_id, "Для разбора тренировки нужен подключённый Garmin или Strava.")
        return
    args = list(context.args or [])
    raw_mode = bool(args) and args[0].lower() in ("data", "raw", "данные")
    if raw_mode:
        args = args[1:]
    simple_mode = bool(args) and args[0].lower() in ("simple", "s")
    if simple_mode:
        args = args[1:]
    selector = selector_override if selector_override else (args[0] if args else None)

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
        msg = await context.bot.send_message(chat_id, "⏳ Собираю пакет данных…")
        try:
            from ai_package import build_package, PROMPT
            res = await build_package(db_user_id, selector)
        except Exception as e:
            logger.error(f"/report data error for {update.effective_user.id}: {e}", exc_info=True)
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
            "⏳ Собираю данные, графики и анализ через ИИ…\nМожет занять 1-3 мин.")
    msg = await context.bot.send_message(chat_id, wait)
    try:
        from ai_package import build_package, PROMPT
        res = await build_package(db_user_id, selector)
    except Exception as e:
        logger.error(f"/report error for {update.effective_user.id}: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка сборки: {type(e).__name__}: {e}")
        return
    if not res.get("ok"):
        await msg.edit_text(f"⚠️ {res.get('msg')}")
        return

    # Картинки: карточка разбора + графики одной вертикальной PNG.
    # Фолбэк на старые 3 PNG, если карточка не построилась.
    chart_items = []
    try:
        from ai_package import build_report_card, build_charts_stacked
        card = await build_report_card(
            res.get("splits"), res.get("plan_steps"), res["name"],
            res.get("wdate"), res.get("wgroup"), res.get("source"),
            res.get("s4"), "/tmp", str(db_user_id), dark=False)
        if card:
            stacked = await build_charts_stacked(
                res.get("splits"), res.get("plan_steps"), res["name"],
                "/tmp", str(db_user_id), dark=False, source=res.get("source") or "")
            chart_items = [(p, c) for p, c in (
                (card, "@DD_adviser_bot · dodick.run"),
                (stacked, "Графики · @DD_adviser_bot · dodick.run")) if p]
    except Exception as e:
        logger.error(f"/report card error: {e}", exc_info=True)
    if not chart_items:
        try:
            from ai_package import build_charts
            charts = await build_charts(res.get("splits"), res.get("plan_steps"),
                                        res["name"], "/tmp", str(db_user_id), dark=False)
        except Exception as e:
            logger.error(f"/report charts error: {e}", exc_info=True)
            charts = {}
        chart_items = [(p, c) for p, c in (
            (charts.get("work_png"), "Рабочие интервалы"),
            (charts.get("rest_png"), "Отдых"),
            (charts.get("table_png"), "Таблица повторов")) if p]

    # Полный режим: получаем анализ ИИ ДО отправки (чтобы отдать всё разом).
    ai_chunks = None
    if not simple_mode:
        # 17.08.2026: разбор в РЕЖИМЕ ПОЛЬЗОВАТЕЛЯ (был всегда deep); calc → fast —
        # текстовый разбор нужен всем, у формульного режима своего ИИ-пути нет.
        _rmode = (get_preferences(db_user_id) or {}).get("ai_mode", "smart")
        _rmode = {"calc": "fast"}.get(_rmode, _rmode)
        _rlabel = _MODE_INFO.get(_rmode, ("", _rmode))[1]
        await msg.edit_text(f"🤖 Анализирую через ИИ ({_rlabel})… ({res['name']})")
        import claude_advisor
        answer, _ai_stats = await asyncio.to_thread(
            claude_advisor.ask_text, PROMPT + "\n\n" + res["text"], _rmode, 0.4, True)
        if answer:
            import re as _re_md
            _ans = _re_md.sub(r"\*\*(.+?)\*\*", r"\1", answer)
            _ans = _re_md.sub(r"__(.+?)__", r"\1", _ans)
            _clean = []
            for _ln in _ans.split("\n"):
                _s = _ln.lstrip()
                _s = _re_md.sub(r"^#{1,6}\s*", "", _s)
                _s = _re_md.sub(r"^[\*\-]\s+", "— ", _s)
                _clean.append(_s)
            # 17.08.2026: плашка режим/токены в разборе — как в рекомендации
            _mstr = claude_advisor._MODE_LABELS.get(_ai_stats.get("mode"), "🧠 Глубокий (ИИ)")
            _plaque = (f"⏱ {_ai_stats.get('time_sec', '?')}с | {_mstr} | "
                       f"📥 {_ai_stats.get('input_tokens', '?')} / "
                       f"📤 {_ai_stats.get('output_tokens', '?')} | v{VERSION}\n"
                       f"@DD_adviser_bot · dodick.run")
            _sv_link = ""
            if res.get("source") == "strava" and res.get("act_id"):
                _sv_link = (f"\n🔗 View on Strava: "
                            f"https://www.strava.com/activities/{res['act_id']}")
            ai_chunks = _send_chunks("\n".join(_clean).strip() + _sv_link + "\n\n" + _plaque)
        else:
            ai_chunks = ["⚠️ ИИ не ответил."]

    # Отдаём всё разом: сперва графики, затем текст анализа.
    await msg.edit_text(f"📊 Тренировка: {res['name']}")
    # Кнопку «Главное меню» вешаем на последнее сообщение: в полном режиме —
    # на последний чанк анализа, в simple-режиме — на последнюю фотографию.
    menu_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu_new")]])
    btn_on_last_photo = not ai_chunks
    menu_sent = False
    for idx, (png, cap) in enumerate(chart_items):
        attach = btn_on_last_photo and idx == len(chart_items) - 1
        with open(png, "rb") as f:
            await context.bot.send_photo(
                update.effective_user.id, photo=f, caption=cap,
                reply_markup=menu_btn if attach else None)
        if attach:
            menu_sent = True
    if ai_chunks:
        for i, ch in enumerate(ai_chunks):
            attach = i == len(ai_chunks) - 1
            await context.bot.send_message(
                update.effective_user.id, ch,
                reply_markup=menu_btn if attach else None)
            if attach:
                menu_sent = True
    if not menu_sent:
        await context.bot.send_message(
            update.effective_user.id, "Готово.", reply_markup=menu_btn)


async def report_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report_user (admin) — разбор тренировки выбранного пользователя.
    /report_user — последняя DD; /report_user 18886572975 | /report_user DD_20260612 — конкретная."""
    if update.effective_user.id not in ADMIN_TELEGRAM_IDS:
        return
    selector = context.args[0] if context.args else None
    context.user_data["report_user_selector"] = selector
    users = get_users_list_for_b()
    if not users:
        await update.message.reply_text("Нет пользователей.")
        return
    keyboard = [
        [InlineKeyboardButton(
            u["name"] + (f" (@{u['username']})" if u.get("username") else ""),
            callback_data=f"report_user_{u['db_user_id']}"
        )]
        for u in users
    ]
    title = (f"Разбор тренировки «{selector}» — выбери пользователя:" if selector
             else "Выбери пользователя для разбора тренировки:")
    await update.message.reply_text(
        title,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def report_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбор пользователя → разбор его тренировки (вывод админу)."""
    query = update.callback_query
    if query.from_user.id not in ADMIN_TELEGRAM_IDS:
        await query.answer("Нет доступа.")
        return
    await query.answer()
    target = int(query.data.rsplit("_", 1)[1])
    sel = context.user_data.pop("report_user_selector", None)
    await cmd_report(update, context, target_db_user_id=target, selector_override=sel)


async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка меню «Разбор» → тот же разбор, что /report (без аргументов)."""
    query = update.callback_query
    await query.answer()
    await cmd_report(update, context)


def main():
    init_db()

    # 17.08.2026: concurrent_updates — без него PTB обрабатывает апдейты СТРОГО
    # ПО ОДНОМУ: чей-то deep-/workout на 3-7 минут замораживал КОМАНДЫ ВСЕХ
    # (три /stats без ответа + answerCallbackQuery 400 на протухший колбэк в логе).
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(64).build()

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
    app.add_handler(CommandHandler("cleanup",  cmd_cleanup))
    app.add_handler(CommandHandler("prompt",   cmd_prompt))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("debug_long", cmd_debug_long))
    app.add_handler(CommandHandler("notifications", cmd_notifications))
    app.add_handler(CommandHandler("mailing", cmd_mailing))
    app.add_handler(MessageHandler(filters.VIDEO, admin_video_handler))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("feedback",  cmd_feedback))
    app.add_handler(CommandHandler("ratings",   cmd_ratings))
    app.add_handler(CommandHandler("feedbacks", cmd_feedbacks))
    app.add_handler(CommandHandler("analyze_test", cmd_analyze_test))
    app.add_handler(CommandHandler("brief", cmd_brief))
    app.add_handler(CommandHandler("brief_p", cmd_brief_p))
    app.add_handler(CommandHandler("rebrief", cmd_rebrief))
    app.add_handler(CommandHandler("shadow_caps", cmd_shadow_caps))
    app.add_handler(CommandHandler("resend_evening", cmd_resend_evening))
    app.add_handler(CommandHandler("preprocess_mode", cmd_preprocess_mode))
    app.add_handler(CommandHandler("test_workout", cmd_test_workout))
    app.add_handler(CommandHandler("test_long",    cmd_test_long))
    app.add_handler(CommandHandler("reanalyze",    cmd_reanalyze))
    app.add_handler(CommandHandler("show_analyze",  cmd_show_analyze))
    app.add_handler(CommandHandler("b",         b_self_command))
    app.add_handler(CommandHandler("b_user",    b_command))
    app.add_handler(CommandHandler("a_user",    a_user_command))
    app.add_handler(CommandHandler("w_user",    w_user_command))
    app.add_handler(CommandHandler("w_user_light", w_user_light_command))
    app.add_handler(CommandHandler("l_user",    l_user_command))
    app.add_handler(CommandHandler("p_b",       p_b_self_command))
    app.add_handler(CommandHandler("p_b_user",  p_b_command))
    app.add_handler(CommandHandler("p_a",       p_a_self_command))
    app.add_handler(CommandHandler("p_a_user",  p_a_command))
    app.add_handler(CommandHandler("p_analyze", p_analyze_command))
    app.add_handler(CommandHandler("activity",  cmd_activity))
    app.add_handler(CommandHandler("msg_user",  cmd_msg_user))
    app.add_handler(CommandHandler("msg_service", cmd_msg_service))
    app.add_handler(CommandHandler("profile_user", cmd_profile_user))
    app.add_handler(CommandHandler("howto",     cmd_howto))
    app.add_handler(CommandHandler("last",      cmd_last))
    app.add_handler(CommandHandler("report",    cmd_report))
    app.add_handler(CommandHandler("report_user", report_user_command))
    app.add_handler(CallbackQueryHandler(msg_user_callback,  pattern=r"^msgu_\d+$"))
    app.add_handler(CallbackQueryHandler(msg_service_callback, pattern=r"^msgsvc_\w+$"))
    app.add_handler(CallbackQueryHandler(profile_user_callback, pattern=r"^profile_user_\d+$"))
    app.add_handler(CallbackQueryHandler(b_user_callback,   pattern=r"^b_user_\d+$"))
    app.add_handler(CallbackQueryHandler(a_user_callback,   pattern=r"^a_user_\d+$"))
    app.add_handler(CallbackQueryHandler(w_user_callback,   pattern=r"^w_user_\d+$"))
    app.add_handler(CallbackQueryHandler(w_user_light_callback, pattern=r"^wul_\d+$"))
    app.add_handler(CallbackQueryHandler(report_user_callback, pattern=r"^report_user_\d+$"))
    app.add_handler(CallbackQueryHandler(l_user_callback,   pattern=r"^l_user_\d+$"))
    app.add_handler(CallbackQueryHandler(pb_user_callback,  pattern=r"^pb_user_\d+(_\d{8})?$"))
    app.add_handler(CallbackQueryHandler(pa_user_callback,  pattern=r"^pa_user_\d+$"))
    app.add_handler(CallbackQueryHandler(panalyze_callback, pattern=r"^panalyze_(interval|long)$"))
    app.add_handler(CallbackQueryHandler(report_callback,   pattern=r"^get_report$"))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(global_error_handler)
    # Сквозной логгер активности: group=-1 выполняется НЕЗАВИСИМО от основных хендлеров
    app.add_handler(TypeHandler(Update, _activity_logger), group=-1)

    job_queue = app.job_queue
    job_queue.run_daily(scheduled_recovery_prefetch, time=time(hour=17, minute=0))                       # 20:00 МСК — прогрев к рассылке
    job_queue.run_daily(scheduled_evening,       time=time(hour=18, minute=0))                          # 21:00 МСК (постоянно с 05.09)
    # PTB days: 0=вс, 1=пн … 6=сб → вт/пт = (2, 5), вс = (0,)
    job_queue.run_daily(scheduled_cache_refresh, time=time(hour=2,  minute=0),  days=(2, 5))            # 05:00 МСК вт/пт
    job_queue.run_daily(scheduled_morning,       time=time(hour=4,  minute=0),  days=(2, 5))            # 07:00 МСК вт/пт
    job_queue.run_daily(scheduled_cache_refresh_sunday, time=time(hour=4, minute=15), days=(0,))        # 07:15 МСК вс
    job_queue.run_daily(scheduled_morning_sunday,       time=time(hour=4, minute=30), days=(0,))        # 07:30 МСК вс
    job_queue.run_repeating(scheduled_new_workout_check, interval=1800, first=60)                       # каждые 30 мин
    job_queue.run_repeating(scheduled_wakeup_poll, interval=900, first=120)                              # каждые 15 мин (окно 06:00–09:00 МСК внутри)
    job_queue.run_repeating(check_new_users, interval=300, first=90)                                     # каждые 5 мин — новые записи в users
    job_queue.run_daily(scheduled_brief_comment, time=time(hour=16, minute=5))                           # 19:05 МСК — бриф до рассылки

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