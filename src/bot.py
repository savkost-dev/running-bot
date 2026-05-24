import os
import asyncio
import logging
from datetime import datetime, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
    get_all_users, save_athlete_cache, get_athlete_cache,
    save_user_profile, get_user_profile,
    get_preferences, set_preference,
    save_last_recommendation, get_last_recommendation,
    get_workout_notification, save_workout_notification, get_last_workout_notification,
    get_users_for_notification,
    get_garmin_recovery_cache, save_garmin_recovery_cache,
)
from strava import (
    get_auth_url, get_recent_runs, analyze_fitness, exchange_code,
    ensure_valid_token, get_full_athlete_data
)
from telegram_reader import (
    find_next_workout, find_next_long_run, format_workout_message,
    get_latest_workout_post_id, get_latest_long_run_post_id, get_extra_groups_for_post,
)
import claude_advisor
from claude_advisor import (
    build_evening_prompt, build_morning_prompt, build_long_run_prompt,
    ask_groq, format_evening_message, format_morning_message, format_long_run_message,
)

ADMIN_TELEGRAM_IDS = {273726778}

last_workout: dict | None = None
last_long_run: dict | None = None

# In-memory cache: telegram_id → FIT generation params (set after recommendation)
_fit_data: dict[int, dict] = {}

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
    Получает данные атлета — из кэша (быстро) или обновляет если кэш устарел.
    """
    # Проверяем кэш
    cache = get_athlete_cache(db_user_id)

    # Всегда берём свежие пробежки за 14 дней (быстро — 1 запрос)
    try:
        from strava import get_recent_runs, analyze_fitness
        runs = await get_recent_runs(access_token, days=14)
        fitness = analyze_fitness(runs)
    except Exception as e:
        logger.error(f"Ошибка получения пробежек: {e}")
        fitness = {"summary": "Нет данных", "total_km": 0, "run_count": 0,
                   "avg_pace": "—", "avg_hr": None, "fatigue_level": "unknown"}

    if cache:
        # Добавляем кэшированные данные
        fitness["training_load"] = cache["training_load"]
        fitness["predictions"] = cache["predictions"]
        fitness["last_race"] = cache["last_race"]
    else:
        # Кэша нет — обновляем принудительно
        logger.info(f"Кэш устарел для user_id={db_user_id}, обновляю...")
        athlete_data = await refresh_athlete_cache(db_user_id, access_token)
        if athlete_data:
            fitness["training_load"] = athlete_data["training_load"]
            fitness["predictions"] = athlete_data["predictions"]
            fitness["last_race"] = athlete_data["last_race"]

    return fitness


# ── КОМАНДЫ ───────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = get_or_create_user(user.id, user.full_name, user.username)

    strava = get_token(db_user_id, "strava")
    whoop = get_token(db_user_id, "whoop")
    garmin = get_token(db_user_id, "garmin")
    profile = get_user_profile(db_user_id)

    profile_ok = bool(profile and profile.get("vo2max"))
    recovery_ok = bool(whoop or garmin)
    all_set = profile_ok and bool(strava) and recovery_ok

    keyboard = [
        [InlineKeyboardButton("📋 Тренировка", callback_data="get_workout"),
         InlineKeyboardButton("🕐 Long Run", callback_data="get_long_run")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="my_profile"),
         InlineKeyboardButton("🧠 Режим AI", callback_data="ai_mode")],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="notifications"),
         InlineKeyboardButton("🔄 Обновить данные", callback_data="refresh_cache")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")],
    ]

    if all_set:
        recovery_name = 'Whoop' if whoop else 'Garmin'
        text = (
            f"Привет, {user.first_name}! 👋\n\n"
            "✅ Профиль заполнен\n"
            "✅ Strava подключена\n"
            f"✅ {recovery_name} подключён\n\n"
            "Что умею:\n"
            "🏃 /workout — анализирую форму (CTL/ATL/TSB), восстановление "
            "и рекомендую группу для тренировки вт/пт со шкалой подходимости\n"
            "🕐 /long — то же самое для воскресного Long Run "
            "с рекомендацией стратегии (ровный темп или прогрессия)\n"
            "☀️ /morning — утром в день тренировки проверяю "
            f"{recovery_name} и корректирую план\n"
            "📢 Автоматически уведомляю когда выходит новый анонс тренировки\n\n"
            "Выбери действие 👇"
        )
    else:
        checklist = []
        if profile_ok:
            checklist.append("✅ Профиль заполнен")
        else:
            checklist.append("❌ Заполни профиль — /profile\n    (VO2max и лактатный порог нужны для точных советов)")

        if strava:
            checklist.append("✅ Strava подключена")
        else:
            checklist.append("⚠️ Подключи Strava — /connect_strava\n    (без неё советы по группе приблизительные)")

        if whoop:
            checklist.append("✅ Whoop подключён")
        elif garmin:
            checklist.append("✅ Garmin подключён")
        else:
            checklist.append("💡 Whoop или Garmin — /connect_garmin\n    (опционально, для утренней проверки)")

        text = (
            f"Привет, {user.first_name}! Я помогу подготовиться к тренировкам Dusty Dumbbells.\n\n"
            + "\n".join(checklist)
            + "\n\nВыбери действие 👇"
        )

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = await update.message.reply_text("🔍 Ищу тренировку в канале...")
    await _send_workout_recommendation(user.id, user.full_name, context, msg)


async def cmd_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = await update.message.reply_text("🔍 Ищу Long Run в канале...")
    await _send_long_run_recommendation(user.id, user.full_name, context, msg)


async def cmd_morning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = await update.message.reply_text("☀️ Проверяю твоё восстановление...")
    await _send_morning_check(user.id, context, msg)


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительное обновление кэша данных атлета"""
    user = update.effective_user
    db_user_id = get_or_create_user(user.id, user.full_name, user.username)

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
    text, keyboard = _build_status_text(db_user_id)
    if hasattr(query_or_update, 'edit_message_text'):
        await query_or_update.edit_message_text(text, reply_markup=keyboard)
    else:
        await query_or_update.message.reply_text(text, reply_markup=keyboard)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = get_or_create_user(user.id, user.full_name, user.username)
    await _show_status(update, db_user_id)


async def cmd_connect_strava(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    auth_url = get_auth_url(user.id)
    keyboard = [[InlineKeyboardButton("🔗 Войти в Strava", url=auth_url)]]
    await update.message.reply_text(
        "Нажми кнопку, авторизуйся в Strava.\n\n"
        "После авторизации браузер покажет ошибку — это нормально. "
        "Скопируй весь URL из адресной строки и отправь мне.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["awaiting_strava_code"] = True


async def cmd_connect_whoop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from whoop import get_auth_url as whoop_auth_url
    user = update.effective_user
    auth_url = whoop_auth_url(user.id)
    keyboard = [[InlineKeyboardButton("🔗 Войти в Whoop", url=auth_url)]]
    await update.message.reply_text(
        "Нажми кнопку, авторизуйся в Whoop.\n\n"
        "После авторизации скопируй весь URL из адресной строки и отправь мне.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["awaiting_whoop_code"] = True


async def cmd_connect_garmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Подключение Garmin Connect\n\n"
        "Email и пароль хранятся на сервере в зашифрованном виде (AES-256) — "
        "в открытом виде они нигде не сохраняются.\n\n"
        "Введи email от Garmin Connect:"
    )
    context.user_data["awaiting_garmin"] = "email"


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


def _build_profile_text(profile: dict | None) -> str:
    if not profile or not any([profile.get("vo2max"), profile.get("lactate_threshold_pace"), profile.get("gender")]):
        return "Профиль не заполнен. Используй кнопки ниже чтобы добавить данные."
    lines = ["Твой профиль:\n"]
    if profile.get("gender"):
        lines.append(f"Пол: {'Мужской' if profile['gender'] == 'male' else 'Женский'}")
    if profile.get("vo2max"):
        tag = _vo2max_tag(profile)
        lines.append(f"VO2max: {profile['vo2max']} мл/кг/мин{f'  ({tag})' if tag else ''}")
    if profile.get("lactate_threshold_pace"):
        lt = f"Лактатный порог: {profile['lactate_threshold_pace']} мин/км"
        if profile.get("lactate_threshold_hr"):
            lt += f" при ЧСС {profile['lactate_threshold_hr']} уд/мин"
        lt_source = profile.get("lactate_source")
        if lt_source:
            lt += f"  ({'Garmin' if lt_source == 'garmin' else 'вручную'})"
        lines.append(lt)
    if profile.get("updated_at"):
        lines.append(f"\nОбновлено: {profile['updated_at'][:10]}")
    return '\n'.join(lines)


def _build_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Указать VO2max", callback_data="profile_set_vo2max")],
        [InlineKeyboardButton("🏃 Лактатный порог", callback_data="profile_set_lactate")],
        [InlineKeyboardButton("👤 Пол", callback_data="profile_set_gender")],
    ])


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = get_or_create_user(user.id, user.full_name, user.username)
    profile = get_user_profile(db_user_id)
    await update.message.reply_text(
        _build_profile_text(profile),
        reply_markup=_build_profile_keyboard()
    )


def _build_mode_text(current_mode: str) -> str:
    deep_mark = "✓ " if current_mode == "deep" else ""
    fast_mark = "✓ " if current_mode == "fast" else ""
    return (
        f"{deep_mark}🧠 Глубокое мышление (~1 мин) — с расширенным анализом\n"
        f"{fast_mark}⚡ Быстрый режим (~5 сек) — без расширенного анализа\n\n"
        "Выбери режим:"
    )


def _build_mode_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"{'✓ ' if current_mode == 'deep' else ''}🧠 Глубокое",
            callback_data="mode_set_deep"
        ),
        InlineKeyboardButton(
            f"{'✓ ' if current_mode == 'fast' else ''}⚡ Быстрое",
            callback_data="mode_set_fast"
        ),
    ]])


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = get_or_create_user(user.id, user.full_name, user.username)
    prefs = get_preferences(db_user_id)
    current_mode = prefs.get("ai_mode", "deep") if prefs else "deep"
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
    ])


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_id = get_or_create_user(user.id, user.full_name, user.username)
    prefs = get_preferences(db_user_id)
    await update.message.reply_text(
        _build_notifications_text(prefs),
        reply_markup=_build_notifications_keyboard(prefs)
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin = update.effective_user.id in ADMIN_TELEGRAM_IDS
    text = (
        "Команды бота:\n\n"
        "/workout — ближайшая тренировка (вт/пт) и рекомендация группы\n"
        "/long — воскресный Long Run (100 минут) и рекомендация группы\n"
        "/morning — утренняя проверка восстановления\n"
        "/profile — профиль спортсмена (VO2max, лактатный порог, пол)\n"
        "/mode — режим AI: 🧠 глубокое (~1 мин) или ⚡ быстрое (~5 сек)\n"
        "/refresh — обновить данные из Strava\n"
        "/status — статус подключённых сервисов\n"
        "/connect_strava — подключить Strava\n"
        "/connect_whoop — подключить Whoop\n"
        "/connect_garmin — подключить Garmin Connect\n"
        "/help — эта справка\n\n"
        "Автоматические уведомления:\n"
        "• Вечером накануне тренировки (пн, чт, сб) в 20:00\n"
        "• Утром в день тренировки в 07:00"
    )
    if is_admin:
        text += (
            "\n\n— Админ —\n"
            "/prompt — последний промпт к модели\n"
            "/debug — что распарсил бот из тренировки (интервал)\n"
            "/debug_long — что распарсил бот из Long Run"
        )
    text += f"\n\nv{VERSION}"
    await update.message.reply_text(text)


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


# ── КНОПКИ ───────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    user = update.effective_user

    if query.data == "connect_strava":
        auth_url = get_auth_url(user.id)
        keyboard = [[InlineKeyboardButton("🔗 Войти в Strava", url=auth_url)]]
        await query.edit_message_text(
            "Нажми кнопку, авторизуйся в Strava.\n\n"
            "После авторизации скопируй весь URL из адресной строки и отправь мне.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data["awaiting_strava_code"] = True

    elif query.data == "get_workout":
        await query.edit_message_text("🔍 Ищу тренировку в канале...")
        await _send_workout_recommendation(user.id, user.full_name, context)

    elif query.data == "get_long_run":
        await query.edit_message_text("🔍 Ищу Long Run в канале...")
        await _send_long_run_recommendation(user.id, user.full_name, context)

    elif query.data == "refresh_cache":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        access_token = await ensure_valid_token(db_user_id)
        if not access_token:
            await query.edit_message_text("❌ Strava не подключена. Сначала /connect_strava")
            return
        await query.edit_message_text("⏳ Обновляю данные из Strava (~1 мин)...")
        athlete_data = await refresh_athlete_cache(db_user_id, access_token)
        if athlete_data:
            load = athlete_data["training_load"]
            await query.edit_message_text(
                f"✅ Данные обновлены!\n\n"
                f"CTL: {load.get('ctl', '—')} | "
                f"ATL: {load.get('atl', '—')} | "
                f"TSB: {load.get('tsb', '—')}\n"
                f"{load.get('form_text', '—')}\n"
                f"Тренд: {load.get('trend_text', '—')}"
            )
        else:
            await query.edit_message_text("❌ Не удалось обновить. Попробуй /refresh")

    elif query.data == "my_profile":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        profile = get_user_profile(db_user_id)
        await query.edit_message_text(
            _build_profile_text(profile),
            reply_markup=_build_profile_keyboard()
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
            reply_markup=_build_profile_keyboard()
        )

    elif query.data == "ai_mode":
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        prefs = get_preferences(db_user_id)
        current_mode = prefs.get("ai_mode", "deep") if prefs else "deep"
        await query.edit_message_text(
            _build_mode_text(current_mode),
            reply_markup=_build_mode_keyboard(current_mode)
        )

    elif query.data in ("mode_set_deep", "mode_set_fast"):
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        new_mode = "deep" if query.data == "mode_set_deep" else "fast"
        set_preference(db_user_id, "ai_mode", new_mode)
        mode_label = "🧠 Глубокое мышление" if new_mode == "deep" else "⚡ Быстрый режим"
        await query.edit_message_text(
            f"✅ Режим сохранён: {mode_label}\n\n{_build_mode_text(new_mode)}",
            reply_markup=_build_mode_keyboard(new_mode)
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
        await query.edit_message_text(answer)

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
        is_admin = user.id in ADMIN_TELEGRAM_IDS
        text = (
            "Команды бота:\n\n"
            "/workout — ближайшая тренировка (вт/пт) и рекомендация группы\n"
            "/long — воскресный Long Run (100 минут) и рекомендация группы\n"
            "/morning — утренняя проверка восстановления\n"
            "/profile — профиль спортсмена (VO2max, лактатный порог, пол)\n"
            "/mode — режим AI: 🧠 глубокое (~1 мин) или ⚡ быстрое (~5 сек)\n"
            "/refresh — обновить данные из Strava\n"
            "/status — статус подключённых сервисов\n"
            "/connect_strava — подключить Strava\n"
            "/connect_whoop — подключить Whoop\n"
            "/connect_garmin — подключить Garmin Connect\n"
            "/help — эта справка\n\n"
            "Автоматические уведомления:\n"
            "• Вечером накануне тренировки (пн, чт, сб) в 20:00\n"
            "• Утром в день тренировки в 07:00"
        )
        if is_admin:
            text += (
                "\n\n— Админ —\n"
                "/prompt — последний промпт к модели\n"
                "/debug — что распарсил бот из тренировки (интервал)\n"
                "/debug_long — что распарсил бот из Long Run"
            )
        text += f"\n\nv{VERSION}"
        await query.edit_message_text(text)


# ── ОБРАБОТКА ТЕКСТА ─────────────────────────────────────────

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    # Код Strava
    if context.user_data.get("awaiting_strava_code"):
        import re
        code_match = re.search(r'code=([^&]+)', text)
        code = code_match.group(1) if code_match else text
        msg = await update.message.reply_text("⏳ Подключаю Strava...")
        try:
            token_data = await exchange_code(code)
            if "access_token" not in token_data:
                await msg.edit_text("❌ Не удалось подключить Strava. Попробуй /connect_strava")
                return
            db_user_id = get_or_create_user(user.id, user.full_name, user.username)
            save_token(db_user_id, "strava",
                token_data["access_token"],
                token_data["refresh_token"],
                str(token_data.get("expires_at", ""))
            )
            context.user_data["awaiting_strava_code"] = False

            # Сразу обновляем кэш в фоне
            await msg.edit_text(
                "✅ Strava подключена!\n\n"
                "⏳ Загружаю твои данные (~1 мин)..."
            )
            athlete_data = await refresh_athlete_cache(db_user_id, token_data["access_token"])
            if athlete_data:
                load = athlete_data["training_load"]
                await msg.edit_text(
                    "✅ Strava подключена и данные загружены!\n\n"
                    f"CTL: {load.get('ctl', '—')} | "
                    f"ATL: {load.get('atl', '—')} | "
                    f"TSB: {load.get('tsb', '—')}\n"
                    f"{load.get('form_text', '—')}\n\n"
                    "Попробуй /workout"
                )
            else:
                await msg.edit_text("✅ Strava подключена! Попробуй /workout")

        except Exception as e:
            logger.error(f"Strava auth error: {e}")
            await msg.edit_text("❌ Ошибка. Попробуй /connect_strava")

    # Код Whoop
    elif context.user_data.get("awaiting_whoop_code"):
        import re
        from whoop import exchange_code as whoop_exchange
        code_match = re.search(r'code=([^&]+)', text)
        code = code_match.group(1) if code_match else text
        msg = await update.message.reply_text("⏳ Подключаю Whoop...")
        try:
            import time as time_module
            token_data = await whoop_exchange(code)
            if "access_token" not in token_data:
                await msg.edit_text("❌ Не удалось подключить Whoop. Попробуй /connect_whoop")
                return
            db_user_id = get_or_create_user(user.id, user.full_name, user.username)
            save_token(db_user_id, "whoop",
                token_data["access_token"],
                token_data.get("refresh_token"),
                str(int(time_module.time()) + token_data.get("expires_in", 3600))
            )
            context.user_data["awaiting_whoop_code"] = False
            await msg.edit_text("✅ Whoop подключён! Утренние рекомендации теперь точнее.")
        except Exception as e:
            logger.error(f"Whoop auth error: {e}")
            await msg.edit_text("❌ Ошибка. Попробуй /connect_whoop")

    # Ввод данных профиля
    elif context.user_data.get("awaiting_profile") == "set_vo2max":
        import re
        if not re.match(r'^\d+(?:[.,]\d+)?$', text):
            await update.message.reply_text("Введи число, например: 53")
            return
        vo2max = float(text.replace(',', '.'))
        db_user_id = get_or_create_user(user.id, user.full_name, user.username)
        save_user_profile(db_user_id, vo2max=vo2max, vo2max_source="manual")
        context.user_data.pop("awaiting_profile")
        profile = get_user_profile(db_user_id)
        await update.message.reply_text(
            f"✅ VO2max сохранён: {vo2max} мл/кг/мин\n\n{_build_profile_text(profile)}",
            reply_markup=_build_profile_keyboard()
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
            reply_markup=_build_profile_keyboard()
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

            lines = ["✅ Garmin Connect подключён!\n"]

            if not isinstance(vo2max, Exception) and vo2max is not None:
                save_user_profile(db_user_id, vo2max=vo2max, vo2max_source="garmin")
                lines.append(f"VO2max: {vo2max:.1f} мл/кг/мин  (Garmin)")

            if not isinstance(lt, Exception) and lt:
                save_user_profile(db_user_id,
                    lactate_threshold_pace=lt["pace"],
                    lactate_threshold_hr=lt["hr"],
                    lactate_source="garmin")
                lines.append(f"Лактатный порог: {lt['pace']} мин/км при ЧСС {lt['hr']}  (Garmin)")

            if not isinstance(body_battery, Exception) and body_battery is not None:
                lines.append(f"Body Battery: {body_battery}/100")

            if not isinstance(hrv, Exception) and hrv:
                lines.append(f"ВСР (HRV): {hrv.get('hrv_last_night', '—')} мс (среднее за неделю: {hrv.get('hrv_weekly_avg', '—')})")

            if not isinstance(readiness, Exception) and readiness:
                score = readiness.get("score", "—")
                level = readiness.get("level", "")
                lines.append(f"Training Readiness: {score}/100 ({level})")
                factors = readiness.get("factors") or []
                if factors:
                    lines.append("Что снижает: " + ", ".join(str(f) for f in factors[:3]))

            lines.append(
                "\nТы носишь Garmin постоянно (включая сон)?\n"
                "Это влияет на то, используем ли данные Body Battery и HRV утром."
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, постоянно", callback_data="garmin_recovery_yes"),
                InlineKeyboardButton("🏃 Только на тренировках", callback_data="garmin_recovery_no"),
            ]])
            await msg.edit_text("\n".join(lines), reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Garmin auth error: {e}")
            await msg.edit_text(
                f"❌ Не удалось подключить Garmin Connect.\n"
                f"Проверь правильность email и пароля, затем попробуй /connect_garmin снова.\n\n"
                f"Ошибка: {type(e).__name__}: {e}"
            )


# ── ЛОГИКА РЕКОМЕНДАЦИЙ ──────────────────────────────────────

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

    # 2. Данные Strava из кэша (быстро)
    fitness = None
    access_token = await ensure_valid_token(db_user_id)
    if access_token:
        try:
            fitness = await get_fitness_data(db_user_id, access_token)
        except Exception as e:
            logger.error(f"Strava error for {telegram_id}: {e}")

    if not fitness:
        text = format_workout_message(workout)
        text += "\n\nПодключи Strava (/connect_strava) чтобы получить рекомендацию группы"
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
            if source == "garmin" and updated:
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

    # 4. Данные Whoop
    recovery = await _get_recovery_data(db_user_id)

    # 5. Погода
    weather = await get_weather_for_workout(
        workout.get("location", ""),
        workout.get("workout_date", ""),
        workout.get("schedule", ""),
    )
    weather_line = format_weather_for_message(weather) if weather else ""
    weather_prompt = format_weather_for_prompt(weather) if weather else ""

    # 6. Groq рекомендует
    prefs = get_preferences(db_user_id)
    ai_mode = prefs.get("ai_mode", "deep") if prefs else "deep"
    wait_msg = "🤔 Думаю над рекомендацией... (~1 минута)" if ai_mode == "deep" else "⚡ Получаю рекомендацию..."
    if msg:
        await msg.edit_text(wait_msg)
    prompt = build_evening_prompt(workout, fitness, recovery, weather_prompt=weather_prompt)
    result = ask_groq(prompt, mode=ai_mode)
    advice = result["advice"] if result else None
    stats = result["stats"] if result else None
    text = format_evening_message(advice, workout, stats=stats, weather_line=weather_line)

    if advice:
        try:
            save_last_recommendation(db_user_id, advice, workout)
        except Exception as e:
            logger.error(f"Не удалось сохранить рекомендацию: {e}")

    fit_markup = None
    if msg and advice:
        _fit_data[telegram_id] = {
            'type': 'interval',
            'workout': workout,
            'recommended_group': str(advice.get('recommended_group', '')),
            'recommended_pace': str(advice.get('recommended_pace', '')),
        }
        fit_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Скачать JSON", callback_data="fit_dl"),
            InlineKeyboardButton("⌚ Загрузить в Garmin", callback_data="fit_up"),
        ]])

    if msg:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=fit_markup)
    else:
        await context.bot.send_message(telegram_id, text, parse_mode="HTML")


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

    # Strava из кэша
    fitness = {"summary": "Нет данных", "total_km": 0, "run_count": 0,
               "avg_pace": "—", "avg_hr": None, "fatigue_level": "unknown"}
    access_token = await ensure_valid_token(db_user_id)
    if access_token:
        try:
            fitness = await get_fitness_data(db_user_id, access_token)
        except Exception as e:
            logger.error(f"Strava morning error: {e}")

    # Whoop
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
            hint = "Подключи Whoop или Garmin (/connect_garmin) для точных рекомендаций"
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
    ai_mode = prefs.get("ai_mode", "deep") if prefs else "deep"
    prompt = build_morning_prompt(workout, fitness, recovery, last_rec=last_rec)
    result = ask_groq(prompt, mode=ai_mode)
    advice = result["advice"] if result else None
    text = format_morning_message(advice, last_rec=last_rec)

    if msg:
        await msg.edit_text(text, parse_mode="HTML")
    else:
        await context.bot.send_message(telegram_id, text, parse_mode="HTML")


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
    access_token = await ensure_valid_token(db_user_id)
    if access_token:
        try:
            fitness = await get_fitness_data(db_user_id, access_token)
        except Exception as e:
            logger.error(f"Strava long run error for {telegram_id}: {e}")

    if not fitness:
        text = "🕐 Long Run найден!\n\nПодключи Strava (/connect_strava) для рекомендации группы."
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
            if source == "garmin" and updated:
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

    recovery = await _get_recovery_data(db_user_id)

    # Погода
    weather = await get_weather_for_workout(
        workout.get("location", ""),
        workout.get("workout_date", ""),
        workout.get("schedule", ""),
    )
    weather_line = format_weather_for_message(weather) if weather else ""
    weather_prompt = format_weather_for_prompt(weather) if weather else ""

    prefs = get_preferences(db_user_id)
    ai_mode = prefs.get("ai_mode", "deep") if prefs else "deep"
    wait_msg = "🤔 Думаю над рекомендацией... (~1 минута)" if ai_mode == "deep" else "⚡ Получаю рекомендацию..."
    if msg:
        await msg.edit_text(wait_msg)

    prompt = build_long_run_prompt(workout, fitness, recovery, weather_prompt=weather_prompt)
    result = ask_groq(prompt, mode=ai_mode)
    advice = result["advice"] if result else None
    stats = result["stats"] if result else None
    text = format_long_run_message(advice, workout, stats=stats, weather_line=weather_line)

    fit_markup = None
    if msg and advice:
        _fit_data[telegram_id] = {
            'type': 'long',
            'workout': workout,
            'recommended_group': str(advice.get('recommended_group', '')),
            'strategy': advice.get('run_strategy', 'even'),
            'first_half_pace': str(advice.get('first_half_pace', '')),
            'second_half_pace': advice.get('second_half_pace'),
        }
        fit_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Скачать JSON", callback_data="fit_dl"),
            InlineKeyboardButton("⌚ Загрузить в Garmin", callback_data="fit_up"),
        ]])

    if msg:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=fit_markup)
    else:
        await context.bot.send_message(telegram_id, text, parse_mode="HTML")


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


async def _get_recovery_data(db_user_id: int) -> dict | None:
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
        # Если Garmin носят постоянно — Training Readiness поверх Whoop (из кэша или API)
        if use_garmin and has_garmin:
            try:
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

    # Garmin — кэш (8 ч), при устаревании — живой запрос
    if use_garmin and has_garmin:
        cached = get_garmin_recovery_cache(db_user_id)
        if cached:
            return cached
        return await _fetch_garmin_recovery(db_user_id)

    return None


# ── ПРОВЕРКА НОВЫХ АНОНСОВ ───────────────────────────────────

async def _notify_all(context, text: str, notify_key: str = "") -> int:
    """Рассылает текст пользователям с включённым уведомлением. Возвращает количество успешных."""
    users = get_users_for_notification(notify_key) if notify_key else get_all_users()
    count = 0
    for telegram_id, name, _ in users:
        try:
            await context.bot.send_message(telegram_id, text, parse_mode="HTML")
            count += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Broadcast error for {telegram_id}: {e}")
    return count


async def scheduled_new_workout_check(context: ContextTypes.DEFAULT_TYPE):
    """Каждые 30 минут проверяет новые анонсы тренировок и доп. группы."""

    # ── Вт/Пт тренировка ──────────────────────────────────────
    post_id = await get_latest_workout_post_id()
    if post_id:
        existing = get_workout_notification(post_id)
        if not existing:
            workout = await find_next_workout()
            if workout and not workout.get("is_past"):
                date_fmt, weekday = _fmt_workout_date(workout["workout_date"])
                location = workout.get("location") or "—"
                work_text = (workout.get("work_text") or "").strip()
                work_line = f"\n💪 Работа: {work_text}" if work_text else ""
                text = (
                    f"📢 <b>Вышел анонс тренировки!</b>\n"
                    f"⚡ {weekday} {date_fmt} | 📍 {location}"
                    f"{work_line}\n\n"
                    f"Нажми /workout чтобы получить рекомендацию группы"
                )
                count = await _notify_all(context, text, "notify_interval")
                save_workout_notification(post_id, "interval", workout["workout_date"], [], count)
                logger.info(f"Анонс тренировки {workout['workout_date']}: отправлено {count}")
            else:
                # Прошедшая тренировка при старте — запоминаем без рассылки
                workout_date = workout["workout_date"] if workout else ""
                save_workout_notification(post_id, "interval", workout_date, [], 0)
        else:
            # Проверяем новые доп. группы
            extra_groups = await get_extra_groups_for_post(post_id)
            notified_nums = set(existing.get("notified_extra_groups", []))
            new_groups = [g for g in extra_groups if g["number"] not in notified_nums]
            if new_groups:
                date_fmt, weekday = _fmt_workout_date(existing.get("workout_date", ""))
                for group in new_groups:
                    raw_text = group.get("raw_text", "")[:300]
                    text = (
                        f"➕ <b>Новая группа для тренировки {weekday} {date_fmt}!</b>\n"
                        f"Группа {group['number']}: {raw_text}\n\n"
                        f"Нажми /workout чтобы получить обновлённую рекомендацию"
                    )
                    count = await _notify_all(context, text, "notify_interval_extra")
                    logger.info(f"Новая группа {group['number']} поста {post_id}: отправлено {count}")
                all_notified = list(notified_nums | {g["number"] for g in new_groups})
                save_workout_notification(post_id, "interval", existing["workout_date"], all_notified, 0)

    # ── Воскресный Long Run ────────────────────────────────────
    lr_post_id = await get_latest_long_run_post_id()
    if lr_post_id:
        existing_lr = get_workout_notification(lr_post_id)
        if not existing_lr:
            workout_lr = await find_next_long_run()
            if workout_lr and not workout_lr.get("is_past"):
                date_fmt, weekday = _fmt_workout_date(workout_lr["workout_date"])
                location = workout_lr.get("location") or "—"
                text = (
                    f"📢 <b>Вышел анонс Long Run!</b>\n"
                    f"🕐 {weekday} {date_fmt} | 📍 {location}\n\n"
                    f"Нажми /long чтобы получить рекомендацию группы"
                )
                count = await _notify_all(context, text, "notify_long")
                save_workout_notification(lr_post_id, "long", workout_lr["workout_date"], [], count)
                logger.info(f"Анонс Long Run {workout_lr['workout_date']}: отправлено {count}")
            else:
                workout_date_lr = workout_lr["workout_date"] if workout_lr else ""
                save_workout_notification(lr_post_id, "long", workout_date_lr, [], 0)


# ── ПЛАНИРОВЩИК ──────────────────────────────────────────────

async def scheduled_evening(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    if now.weekday() not in [0, 3, 5]:
        return
    logger.info("Запускаю вечернюю рассылку...")
    users = get_all_users()
    for telegram_id, name, _ in users:
        try:
            await _send_workout_recommendation(telegram_id, name, context)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Evening notification error for {telegram_id}: {e}")


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
                        save_user_profile(db_user_id, vo2max=vo2max, vo2max_source="garmin")
                        vo2max_ok += 1
                    if not isinstance(lt, Exception) and lt:
                        save_user_profile(db_user_id,
                                          lactate_threshold_pace=lt["pace"],
                                          lactate_threshold_hr=lt["hr"],
                                          lactate_source="garmin")
            except Exception as e:
                logger.error(f"Garmin VO2max refresh error for {telegram_id}: {e}")

        await asyncio.sleep(1)

    logger.info(f"Обновление завершено: Strava={strava_ok}, Garmin recovery={garmin_ok}, VO2max={vo2max_ok}")


async def scheduled_morning(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    if now.weekday() not in [1, 4, 6]:
        return
    logger.info("Запускаю утреннюю рассылку...")
    users = get_all_users()
    for telegram_id, name, _ in users:
        try:
            await _send_morning_check(telegram_id, context)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Morning notification error for {telegram_id}: {e}")


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
    app.add_handler(CommandHandler("prompt", cmd_prompt))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("debug_long", cmd_debug_long))
    app.add_handler(CommandHandler("notifications", cmd_notifications))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    job_queue = app.job_queue
    job_queue.run_daily(scheduled_evening, time=time(hour=17, minute=0))   # 20:00 МСК
    job_queue.run_daily(scheduled_morning, time=time(hour=4, minute=0))   # 07:00 МСК
    job_queue.run_repeating(scheduled_new_workout_check, interval=1800, first=60)  # каждые 30 мин
    job_queue.run_daily(scheduled_data_refresh, time=time(hour=3, minute=0))    # 03:00 UTC = 06:00 МСК

    logger.info("✅ Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()