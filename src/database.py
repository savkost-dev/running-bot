import sqlite3
import os
from cryptography.fernet import Fernet
from datetime import datetime
from dotenv import load_dotenv
import json as _json

load_dotenv()

# Загружаем ключ шифрования из .env
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
fernet = Fernet(ENCRYPTION_KEY.encode())

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'running_bot.db')


def get_connection():
    return sqlite3.connect(DB_PATH)


def count_service_tokens(service: str) -> int:
    """Сколько пользователей сейчас имеют сохранённый токен сервиса.
    Для Strava это = занятые слоты в лимите приложения (athlete connections)."""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM user_tokens "
            "WHERE service = ? AND access_token IS NOT NULL",
            (service,))
        return cur.fetchone()[0]


def init_db():
    """Создаём таблицы при первом запуске"""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                name TEXT,
                username TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                service TEXT NOT NULL,
                access_token TEXT,
                refresh_token TEXT,
                expires_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id),
                UNIQUE(user_id, service)
            );

            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY,
                default_group TEXT,
                notify_evening INTEGER DEFAULT 1,
                notify_morning INTEGER DEFAULT 1,
                ai_mode TEXT DEFAULT 'smart',
                is_active INTEGER DEFAULT 1,
                deactivated_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
                           
            CREATE TABLE IF NOT EXISTS athlete_cache (
                user_id INTEGER PRIMARY KEY,
                ctl REAL,
                atl REAL,
                tsb REAL,
                trend_text TEXT,
                form_text TEXT,
                predictions TEXT,
                last_race TEXT,
                pace_zones_json TEXT,
                zones_source TEXT,
                zones_updated_at TEXT,
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS user_profile (
                user_id INTEGER PRIMARY KEY,
                vo2max REAL,
                lactate_threshold_pace TEXT,
                lactate_threshold_hr INTEGER,
                gender TEXT,
                specialization TEXT DEFAULT 'half_marathon',
                vo2max_locked INTEGER DEFAULT 0,
                lactate_locked INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS garmin_recovery_cache (
                user_id INTEGER PRIMARY KEY,
                body_battery INTEGER,
                hrv_last_night REAL,
                hrv_weekly_avg REAL,
                hrv_status TEXT,
                tr_score INTEGER,
                tr_level TEXT,
                tr_factors TEXT,
                updated_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS workout_notifications (
                post_id INTEGER PRIMARY KEY,
                post_type TEXT DEFAULT 'interval',
                workout_date TEXT,
                notified_at TEXT,
                notified_extra_groups TEXT DEFAULT '[]',
                users_notified INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS last_recommendation (
                user_id INTEGER PRIMARY KEY,
                recommended_group TEXT,
                recommended_pace TEXT,
                reason TEXT,
                if_feeling_good TEXT,
                if_tired TEXT,
                workout_date TEXT,
                workout_title TEXT,
                groups_raw TEXT,
                extra_groups_raw TEXT,
                saved_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS user_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                command TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS recommendation_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                workout_date TEXT,
                rating INTEGER NOT NULL,
                ai_mode TEXT,
                comment TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS workout_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER UNIQUE NOT NULL,
                workout_date TEXT,
                workout_type TEXT,
                is_valid INTEGER DEFAULT 0,
                raw_text TEXT,
                analyzed_json TEXT,
                extra_groups_json TEXT,
                analysis_mode TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                edit_date TEXT
            );

            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            -- Эталоны тренировок по группам (готовый Garmin JSON для быстрой отправки).
            -- Пишутся после вечерней рассылки в интервальные дни. Ключ: дата + группа + тип.
            CREATE TABLE IF NOT EXISTS workout_templates (
                workout_date TEXT NOT NULL,
                group_number TEXT NOT NULL,
                wtype        TEXT NOT NULL,
                workout_json TEXT NOT NULL,
                created_at   TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (workout_date, group_number, wtype)
            );

            CREATE TABLE IF NOT EXISTS raw_service_data (
                user_id INTEGER NOT NULL,
                service TEXT NOT NULL,
                raw_json TEXT,
                fetched_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, service),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS unified_cache (
                user_id    INTEGER PRIMARY KEY,
                unified_json TEXT NOT NULL,
                sources    TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            -- История утренних слепков: структура как unified_cache, но без unified_json
            -- и с датой в ключе (одна строка на (user_id, date), новая запись каждый день).
            CREATE TABLE IF NOT EXISTS mornings (
                user_id             INTEGER NOT NULL,
                date                TEXT NOT NULL,
                sources             TEXT,
                morning_caught      INTEGER DEFAULT 0,
                morning_tr          INTEGER,
                morning_bb          INTEGER,
                morning_hrv         REAL,
                morning_rhr         INTEGER,
                morning_sleep_h     REAL,
                morning_wake_at     TEXT,
                morning_snapshot_at TEXT,
                PRIMARY KEY (user_id, date),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

        """)
    # Миграции для старых БД
    with get_connection() as conn:
        try:
            conn.execute("ALTER TABLE user_profile ADD COLUMN gender TEXT")
        except Exception:
            pass
    with get_connection() as conn:
        try:
            conn.execute("ALTER TABLE user_preferences ADD COLUMN ai_mode TEXT DEFAULT 'smart'")
        except Exception:
            pass
    with get_connection() as conn:
        try:
            conn.execute("ALTER TABLE user_preferences ADD COLUMN use_garmin_recovery INTEGER DEFAULT 1")
        except Exception:
            pass
    for col in ("is_active INTEGER DEFAULT 1", "deactivated_at TEXT"):
        with get_connection() as conn:
            try:
                conn.execute(f"ALTER TABLE user_preferences ADD COLUMN {col}")
            except Exception:
                pass
    for col in ("notify_interval INTEGER DEFAULT 1",
                "notify_interval_extra INTEGER DEFAULT 1",
                "notify_long INTEGER DEFAULT 1",
                "notify_morning_interval INTEGER DEFAULT 1",
                "notify_morning_long INTEGER DEFAULT 1"):
        with get_connection() as conn:
            try:
                conn.execute(f"ALTER TABLE user_preferences ADD COLUMN {col}")
            except Exception:
                pass
    for col in ("garmin_email TEXT", "garmin_password TEXT",
                "vo2max_source TEXT", "vo2max_updated_at TEXT",
                "lactate_source TEXT",
                "coros_email TEXT", "coros_password TEXT",
                "polar_user_id TEXT",
                "specialization TEXT DEFAULT 'half_marathon'",
                "vo2max_locked INTEGER DEFAULT 0",
                "lactate_locked INTEGER DEFAULT 0",
                "birthdate TEXT",
                "coros_region TEXT",
                "vo2max_device REAL",
                "vo2max_device_source TEXT",
                "vo2max_device_at TEXT",
                "vo2max_manual REAL",
                "vo2max_manual_at TEXT",
                "vo2max_priority TEXT",
                "lt_pace_device TEXT",
                "lt_hr_device INTEGER",
                "lt_device_source TEXT",
                "lt_device_at TEXT",
                "lt_pace_manual TEXT",
                "lt_hr_manual INTEGER",
                "lt_manual_at TEXT",
                "lt_priority TEXT"):
        with get_connection() as conn:
            try:
                conn.execute(f"ALTER TABLE user_profile ADD COLUMN {col}")
            except Exception:
                pass
    for col in ("pace_zones_json TEXT", "zones_source TEXT", "zones_updated_at TEXT"):
        with get_connection() as conn:
            try:
                conn.execute(f"ALTER TABLE athlete_cache ADD COLUMN {col}")
            except Exception:
                pass
    # Strava webhooks: связка событий (owner_id = athlete id) с нашим user_id
    with get_connection() as conn:
        try:
            conn.execute("ALTER TABLE user_tokens ADD COLUMN strava_athlete_id TEXT")
        except Exception:
            pass
    with get_connection() as conn:
        try:
            conn.execute("ALTER TABLE workout_analysis ADD COLUMN edit_date TEXT")
        except Exception:
            pass
    with get_connection() as conn:
        try:
            conn.execute("ALTER TABLE last_recommendation ADD COLUMN ai_mode TEXT")
        except Exception:
            pass
    with get_connection() as conn:
        try:
            conn.execute("ALTER TABLE last_recommendation ADD COLUMN evening_recovery_score INTEGER")
        except Exception:
            pass
    with get_connection() as conn:
        try:
            conn.execute("ALTER TABLE last_recommendation ADD COLUMN lowered_by_recovery INTEGER DEFAULT 0")
        except Exception:
            pass

    # Маркеры "поймали полную ночь сегодня" в unified_cache
    for col in ("morning_caught INTEGER DEFAULT 0", "morning_date TEXT"):
        with get_connection() as conn:
            try:
                conn.execute(f"ALTER TABLE unified_cache ADD COLUMN {col}")
            except Exception:
                pass

    # Снимок "на утро" (замораживается при поимке ночи, день не перетирает)
    for col in ("morning_tr INTEGER",          # Training Readiness score
                "morning_bb INTEGER",          # Body Battery на пробуждении
                "morning_hrv REAL",            # ночной HRV
                "morning_rhr INTEGER",         # ЧСС покоя
                "morning_sleep_h REAL",        # длина сна, ч
                "morning_wake_at TEXT",        # время пробуждения (локальное ISO)
                "morning_snapshot_at TEXT"):   # когда снят снимок (UTC ISO)
        with get_connection() as conn:
            try:
                conn.execute(f"ALTER TABLE unified_cache ADD COLUMN {col}")
            except Exception:
                pass

    # Дефолт глобальной настройки режима анализа тренировок
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('preprocess_mode', 'deep')"
        )

    # Разовое копирование текущих слепков из unified_cache в mornings.
    # INSERT OR IGNORE по PK (user_id, date) — повторные рестарты не плодят дублей
    # и не затирают уже накопленную историю. Берём только строки с пойманной ночью.
    with get_connection() as conn:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO mornings
                    (user_id, date, sources, morning_caught, morning_tr, morning_bb,
                     morning_hrv, morning_rhr, morning_sleep_h, morning_wake_at, morning_snapshot_at)
                SELECT user_id, morning_date, sources, morning_caught, morning_tr, morning_bb,
                       morning_hrv, morning_rhr, morning_sleep_h, morning_wake_at, morning_snapshot_at
                FROM unified_cache
                WHERE morning_date IS NOT NULL
            """)
        except Exception:
            pass

    print("✅ База данных инициализирована")


# ── Пользователи ──────────────────────────────────────────────

def get_or_create_user(telegram_id: int, name: str, username: str = None) -> int:
    """Возвращает id пользователя, создаёт если не существует"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()

        if row:
            return row[0]

        cursor = conn.execute(
            "INSERT INTO users (telegram_id, name, username) VALUES (?, ?, ?)",
            (telegram_id, name, username)
        )
        conn.execute(
            "INSERT INTO user_preferences (user_id) VALUES (?)",
            (cursor.lastrowid,)
        )
        print(f"✅ Новый пользователь: {name} ({telegram_id})")
        return cursor.lastrowid


def user_exists(telegram_id: int) -> bool:
    """Проверяет, зарегистрирован ли пользователь."""
    with get_connection() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone())


def get_all_users() -> list:
    """Список всех пользователей (для рассылки)"""
    with get_connection() as conn:
        return conn.execute(
            "SELECT telegram_id, name, username FROM users"
        ).fetchall()


def get_active_users() -> list:
    """Список активных пользователей (is_active=1) для рассылки."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT u.telegram_id, u.name, u.username
            FROM users u
            LEFT JOIN user_preferences p ON u.id = p.user_id
            WHERE p.is_active IS NULL OR p.is_active = 1
        """).fetchall()


# ── Strava webhooks: маппинг athlete_id ↔ user_id ──────────────────

def set_strava_athlete_id(user_id: int, athlete_id) -> None:
    """Запоминает Strava athlete id для пользователя (нужен вебхукам:
    события приходят с owner_id, без нашего user_id)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE user_tokens SET strava_athlete_id = ? "
            "WHERE user_id = ? AND service = 'strava'",
            (str(athlete_id), user_id))


def get_user_by_strava_athlete_id(athlete_id) -> int | None:
    """user_id по Strava athlete id (для событий вебхука). None если не знаем."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM user_tokens "
            "WHERE service = 'strava' AND strava_athlete_id = ?",
            (str(athlete_id),)).fetchone()
    return row[0] if row else None


def purge_strava_data(user_id: int) -> None:
    """Полная очистка Strava-данных пользователя при деавторизации
    (Strava API Policy 7.4: удалять не только токен, а все Strava Data
    и производные). Удаляет: токен; athlete_cache (CTL/ATL/TSB и прогнозы
    считаны из strava-активностей); raw_service_data('strava'); строку
    unified_cache (смешанные источники — пересоберётся ночным джобом
    из оставшихся сервисов без strava)."""
    with get_connection() as conn:
        conn.execute("DELETE FROM user_tokens WHERE user_id = ? AND service = 'strava'",
                     (user_id,))
        conn.execute("DELETE FROM athlete_cache WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM raw_service_data WHERE user_id = ? AND service = 'strava'",
                     (user_id,))
        conn.execute("DELETE FROM unified_cache WHERE user_id = ?", (user_id,))


def get_inactive_users() -> list:
    """Пользователи с is_active=0 (заблокировали бота)."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT u.telegram_id, u.name, u.username
            FROM users u
            JOIN user_preferences p ON u.id = p.user_id
            WHERE p.is_active = 0
        """).fetchall()


def get_all_users_with_status(notify_key: str = "") -> list:
    """Активные пользователи с флагом has_data.
    has_data=1 если есть vo2max в профиле ИЛИ хотя бы один токен трекера (strava/garmin/coros/polar).
    Если указан notify_key — дополнительно фильтрует по настройке уведомлений.
    Возвращает список кортежей (telegram_id, name, username, has_data).
    """
    notify_filter = (
        f"AND (p.{notify_key} IS NULL OR p.{notify_key} = 1)" if notify_key else ""
    )
    with get_connection() as conn:
        return conn.execute(f"""
            SELECT u.telegram_id, u.name, u.username,
                   CASE WHEN (
                       (pr.vo2max IS NOT NULL)
                       OR EXISTS (
                           SELECT 1 FROM user_tokens t
                           WHERE t.user_id = u.id
                             AND t.service IN ('strava', 'garmin', 'coros', 'polar')
                       )
                   ) THEN 1 ELSE 0 END AS has_data
            FROM users u
            LEFT JOIN user_preferences p ON u.id = p.user_id
            LEFT JOIN user_profile pr    ON u.id = pr.user_id
            WHERE (p.is_active IS NULL OR p.is_active = 1)
            {notify_filter}
        """).fetchall()


def get_users_for_notification(notify_key: str) -> list:
    """Пользователи у которых включён данный тип уведомлений (notify_interval / notify_interval_extra / notify_long).
    Автоматически фильтрует неактивных пользователей (is_active=0).
    """
    with get_connection() as conn:
        return conn.execute(f"""
            SELECT u.telegram_id, u.name, u.username
            FROM users u
            LEFT JOIN user_preferences p ON u.id = p.user_id
            WHERE (p.{notify_key} IS NULL OR p.{notify_key} = 1)
              AND (p.is_active IS NULL OR p.is_active = 1)
        """).fetchall()


# ── Токены ───────────────────────────────────────────────────

def _encrypt(value: str) -> str:
    if not value:
        return None
    return fernet.encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    if not value:
        return None
    return fernet.decrypt(value.encode()).decode()


def save_token(user_id: int, service: str, access_token: str,
               refresh_token: str = None, expires_at: str = None):
    """Сохраняет или обновляет токен сервиса (зашифрованный)"""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO user_tokens (user_id, service, access_token, refresh_token, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, service) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at = excluded.expires_at
        """, (
            user_id, service,
            _encrypt(access_token),
            _encrypt(refresh_token),
            expires_at
        ))


def get_token(user_id: int, service: str) -> dict | None:
    """Возвращает расшифрованный токен или None"""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT access_token, refresh_token, expires_at
            FROM user_tokens
            WHERE user_id = ? AND service = ?
        """, (user_id, service)).fetchone()

    if not row:
        return None

    return {
        "access_token": _decrypt(row[0]),
        "refresh_token": _decrypt(row[1]),
        "expires_at": row[2]
    }


def get_users_with_service(service: str) -> list:
    """Все пользователи у которых подключён данный сервис"""
    with get_connection() as conn:
        return conn.execute("""
            SELECT u.telegram_id, u.name, ut.user_id
            FROM users u
            JOIN user_tokens ut ON u.id = ut.user_id
            WHERE ut.service = ?
        """, (service,)).fetchall()


# ── Настройки ─────────────────────────────────────────────────

def set_preference(user_id: int, key: str, value):
    with get_connection() as conn:
        conn.execute(
            f"UPDATE user_preferences SET {key} = ? WHERE user_id = ?",
            (value, user_id)
        )


def get_preferences(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT default_group, notify_evening, notify_morning, ai_mode, use_garmin_recovery,
                   notify_interval, notify_interval_extra, notify_long,
                   is_active, deactivated_at,
                   notify_morning_interval, notify_morning_long
            FROM user_preferences WHERE user_id = ?
        """, (user_id,)).fetchone()

    if not row:
        return None

    return {
        "default_group": row[0],
        "notify_evening": bool(row[1]),
        "notify_morning": bool(row[2]),
        "ai_mode": row[3] or "smart",
        "use_garmin_recovery": bool(row[4]) if row[4] is not None else True,
        "notify_interval": bool(row[5]) if row[5] is not None else True,
        "notify_interval_extra": bool(row[6]) if row[6] is not None else True,
        "notify_long": bool(row[7]) if row[7] is not None else True,
        "is_active": bool(row[8]) if row[8] is not None else True,
        "deactivated_at": row[9],
        # 17.08.2026: утренние уведомления — отдельные галочки от вечерних
        "notify_morning_interval": bool(row[10]) if row[10] is not None else True,
        "notify_morning_long": bool(row[11]) if row[11] is not None else True,
    }

def log_activity(user_id: int, command: str) -> None:
    """Логирует вызов команды пользователем."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_activity (user_id, command) VALUES (?, ?)",
            (user_id, command)
        )


def get_activity_daily(days: int = 14) -> list:
    """Активность по дням (дни МСК, +3ч к UTC в created_at).
    Возвращает [(date, uniq_users, actions)] от новых к старым."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT date(created_at, '+3 hours') AS d, "
            "COUNT(DISTINCT user_id), COUNT(*) "
            "FROM user_activity "
            "WHERE created_at >= datetime('now', ?) "
            "GROUP BY d ORDER BY d DESC",
            (f"-{int(days)} days",)
        ).fetchall()


def get_activity_top(days: int = 14, limit: int = 12) -> list:
    """Топ действий за период: [(command, actions, uniq_users)] по убыванию."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT command, COUNT(*) AS cnt, COUNT(DISTINCT user_id) "
            "FROM user_activity "
            "WHERE created_at >= datetime('now', ?) "
            "GROUP BY command ORDER BY cnt DESC LIMIT ?",
            (f"-{int(days)} days", int(limit))
        ).fetchall()


def get_activity_users(days: int = 14) -> list:
    """Кто активен за период: [(name, username, actions, last_date_msk)] по убыванию даты последней активности."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT COALESCE(u.name, u.username, 'user_' || a.user_id), u.username, "
            "COUNT(*) AS cnt, MAX(date(a.created_at, '+3 hours')) "
            "FROM user_activity a JOIN users u ON u.id = a.user_id "
            "WHERE a.created_at >= datetime('now', ?) "
            "GROUP BY a.user_id ORDER BY MAX(a.created_at) DESC, cnt DESC",
            (f"-{int(days)} days",)
        ).fetchall()


def get_activity_report(days: int = 14) -> tuple:
    """Использование /report за период: (actions, uniq_users).
    Учитывает и команду, и нажатия кнопки меню (включая прежние btn:get_report)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM user_activity "
            "WHERE command IN ('/report', 'btn:get_report') "
            "AND created_at >= datetime('now', ?)",
            (f"-{int(days)} days",)
        ).fetchone()
    return (row[0], row[1]) if row else (0, 0)


def get_report_users(days: int = 14) -> list:
    """Кто запускал разбор за период — команда /report и кнопка меню.

    [(name, username, сколько раз, последняя дата), ...] по убыванию числа запусков.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT COALESCE(u.name, '—'), u.username, COUNT(*), "
            "MAX(date(a.created_at)) "
            "FROM user_activity a JOIN users u ON u.id = a.user_id "
            "WHERE a.command IN ('/report', 'btn:get_report') "
            "AND a.created_at >= datetime('now', ?) "
            "GROUP BY a.user_id ORDER BY COUNT(*) DESC",
            (f"-{int(days)} days",)
        ).fetchall()
    return [tuple(r) for r in rows]


def get_bot_stats() -> dict:
    """Возвращает агрегированную статистику для команды /stats."""
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        new_7d = conn.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
        active_7d = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM user_activity "
            "WHERE created_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
        active_bot = conn.execute(
            "SELECT COUNT(*) FROM user_preferences WHERE is_active IS NULL OR is_active = 1"
        ).fetchone()[0]
        inactive_bot = conn.execute(
            "SELECT COUNT(*) FROM user_preferences WHERE is_active = 0"
        ).fetchone()[0]
        strava = conn.execute(
            "SELECT COUNT(*) FROM user_tokens WHERE service = 'strava'"
        ).fetchone()[0]
        whoop = conn.execute(
            "SELECT COUNT(*) FROM user_tokens WHERE service = 'whoop'"
        ).fetchone()[0]
        garmin = conn.execute(
            "SELECT COUNT(*) FROM user_tokens WHERE service = 'garmin'"
        ).fetchone()[0]
        coros = conn.execute(
            "SELECT COUNT(*) FROM user_tokens WHERE service = 'coros'"
        ).fetchone()[0]
        polar = conn.execute(
            "SELECT COUNT(*) FROM user_tokens WHERE service = 'polar'"
        ).fetchone()[0]
        profile = conn.execute(
            "SELECT COUNT(*) FROM user_profile WHERE vo2max IS NOT NULL"
        ).fetchone()[0]
        workout_7d, workout_users_7d = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM user_activity "
            "WHERE command = '/workout' AND created_at >= datetime('now', '-7 days')"
        ).fetchone()
        long_7d, long_users_7d = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM user_activity "
            "WHERE command = '/long' AND created_at >= datetime('now', '-7 days')"
        ).fetchone()
        morning_7d, morning_users_7d = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM user_activity "
            "WHERE command = '/morning' AND created_at >= datetime('now', '-7 days')"
        ).fetchone()
        report_7d, report_users_7d = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM user_activity "
            "WHERE command IN ('/report', 'btn:get_report') "
            "AND created_at >= datetime('now', '-7 days')"
        ).fetchone()
        avg_rating_row = conn.execute(
            "SELECT AVG(rating), COUNT(*) FROM recommendation_ratings "
            "WHERE created_at >= datetime('now', '-30 days')"
        ).fetchone()
        avg_rating = round(avg_rating_row[0], 1) if avg_rating_row[0] else None
        ratings_30d = avg_rating_row[1] if avg_rating_row[1] else 0
        feedback_total = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        feedback_bugs = conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE type = 'bug'"
        ).fetchone()[0]
        feedback_features = conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE type = 'feature'"
        ).fetchone()[0]
    return {
        "total": total, "new_7d": new_7d, "active_7d": active_7d,
        "active_bot": active_bot, "inactive_bot": inactive_bot,
        "strava": strava, "whoop": whoop, "garmin": garmin, "coros": coros, "polar": polar,
        "profile": profile,
        "workout_7d": workout_7d, "long_7d": long_7d, "morning_7d": morning_7d,
        "report_7d": report_7d,
        "workout_users_7d": workout_users_7d, "long_users_7d": long_users_7d,
        "morning_users_7d": morning_users_7d, "report_users_7d": report_users_7d,
        "avg_rating": avg_rating, "ratings_30d": ratings_30d,
        "feedback_total": feedback_total, "feedback_bugs": feedback_bugs,
        "feedback_features": feedback_features,
    }


def get_stats_overview() -> dict:
    """Единая статистика для /stats с СОГЛАСОВАННЫМИ определениями (09.08.2026):
    все счётчики — по АКТИВНЫМ пользователям; «якорь для зон» — по resolved-полям v2
    (device/manual, плюс legacy-поля как фолбэк — та же логика, что zones.resolve_anchor);
    воронка данных сходится по построению: tracker + profile_only + empty = active.

    Возвращает: total, active, blocked, tracker_users, profile_only, empty,
    anchor_users, zones_users, services {name: set(uid)}, last_reco
    {date, wtype, by_group {gr: n}, rec_total, subscribed}.
    """
    with get_connection() as conn:
        active_ids = {r[0] for r in conn.execute("""
            SELECT u.id FROM users u
            LEFT JOIN user_preferences p ON u.id = p.user_id
            WHERE p.is_active IS NULL OR p.is_active = 1
        """)}
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

        services: dict[str, set] = {}
        for uid, svc in conn.execute(
                "SELECT user_id, service FROM user_tokens"):
            if uid in active_ids:
                services.setdefault(svc, set()).add(uid)
        tracker_ids = set().union(*services.values()) if services else set()

        anchor_ids = {r[0] for r in conn.execute("""
            SELECT user_id FROM user_profile
            WHERE vo2max_device IS NOT NULL OR vo2max_manual IS NOT NULL
               OR lt_pace_device IS NOT NULL OR lt_pace_manual IS NOT NULL
               OR vo2max IS NOT NULL OR lactate_threshold_pace IS NOT NULL
        """) if r[0] in active_ids}
        zones_ids = {r[0] for r in conn.execute(
            "SELECT user_id FROM athlete_cache WHERE pace_zones_json IS NOT NULL"
        ) if r[0] in active_ids}

        profile_only = anchor_ids - tracker_ids
        empty_n = len(active_ids) - len(tracker_ids | anchor_ids)

        # Готовность: у скольких реально ловится утро (таблица mornings)
        morning_7d = len({r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM mornings "
            "WHERE morning_caught = 1 AND date >= date('now', '-7 days')"
        ) if r[0] in active_ids})
        morning_today = len({r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM mornings "
            "WHERE morning_caught = 1 AND date = date('now')"
        ) if r[0] in active_ids})

        # Последняя рассылка рекомендаций: дата из last_recommendation,
        # тип и подписчики — чтобы было видно «рекомендаций X из Y получателей».
        last_reco = None
        row = conn.execute(
            "SELECT workout_date FROM last_recommendation "
            "ORDER BY workout_date DESC LIMIT 1").fetchone()
        if row and row[0]:
            bdate = row[0]
            by_group: dict[str, int] = {}
            rec_uids = set()
            for uid, grp in conn.execute(
                    "SELECT user_id, recommended_group FROM last_recommendation "
                    "WHERE workout_date = ?", (bdate,)):
                rec_uids.add(uid)
                by_group[str(grp or "—")] = by_group.get(str(grp or "—"), 0) + 1
            wtype_row = conn.execute(
                "SELECT post_type FROM workout_notifications "
                "WHERE workout_date = ? ORDER BY notified_at DESC LIMIT 1",
                (bdate,)).fetchone()
            wtype = (wtype_row[0] if wtype_row else None) or "?"
            notify_key = "notify_long" if wtype == "long" else "notify_interval"
            subscribed = conn.execute(f"""
                SELECT COUNT(*) FROM users u
                LEFT JOIN user_preferences p ON u.id = p.user_id
                WHERE (p.is_active IS NULL OR p.is_active = 1)
                  AND (p.{notify_key} IS NULL OR p.{notify_key} = 1)
            """).fetchone()[0]
            last_reco = {"date": bdate, "wtype": wtype, "by_group": by_group,
                         "rec_total": len(rec_uids), "subscribed": subscribed}

    return {
        "total": total, "active": len(active_ids), "blocked": total - len(active_ids),
        "tracker_users": len(tracker_ids), "profile_only": len(profile_only),
        "empty": empty_n, "anchor_users": len(anchor_ids), "zones_users": len(zones_ids),
        "morning_7d": morning_7d, "morning_today": morning_today,
        "services": services, "last_reco": last_reco,
    }


def get_all_users_with_details() -> list:
    """Все пользователи с деталями: (db_id, telegram_id, name, username, created_at), сортировка по id."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT id, telegram_id, name, username, created_at FROM users ORDER BY id"
        ).fetchall()


def get_users_with_service_full(service: str) -> list:
    """Пользователи с подключённым сервисом: (telegram_id, name, username)."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT u.telegram_id, u.name, u.username
            FROM users u
            JOIN user_tokens ut ON u.id = ut.user_id
            WHERE ut.service = ?
            ORDER BY u.id
        """, (service,)).fetchall()


def get_users_with_profile_full() -> list:
    """Пользователи с заполненным профилем: (telegram_id, name, username)."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT u.telegram_id, u.name, u.username
            FROM users u
            JOIN user_profile up ON u.id = up.user_id
            WHERE up.vo2max IS NOT NULL OR up.lactate_threshold_pace IS NOT NULL
            ORDER BY u.id
        """).fetchall()


def delete_token(user_id: int, service: str) -> None:
    """Удаляет токен сервиса для пользователя (отключение сервиса)."""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM user_tokens WHERE user_id = ? AND service = ?",
            (user_id, service)
        )


def count_users_with_service(service: str) -> int:
    """Количество пользователей с подключённым сервисом."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM user_tokens WHERE service = ?", (service,)
        ).fetchone()[0]


def get_user_display(telegram_id: int) -> str:
    """Возвращает 'Имя (@username)' или просто 'Имя' для telegram_id."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT name, username FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    if not row:
        return str(telegram_id)
    name = row[0] or "Unknown"
    suffix = f" (@{row[1]})" if row[1] else ""
    return f"{name}{suffix}"


def save_athlete_cache(user_id: int, training_load: dict, predictions: dict, last_race: dict | None):
    """Сохраняет кэш данных атлета"""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO athlete_cache 
                (user_id, ctl, atl, tsb, trend_text, form_text, predictions, last_race, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                ctl = excluded.ctl,
                atl = excluded.atl,
                tsb = excluded.tsb,
                trend_text = excluded.trend_text,
                form_text = excluded.form_text,
                predictions = excluded.predictions,
                last_race = excluded.last_race,
                updated_at = excluded.updated_at
        """, (
            user_id,
            training_load.get("ctl"),
            training_load.get("atl"),
            training_load.get("tsb"),
            training_load.get("trend_text"),
            training_load.get("form_text"),
            _json.dumps(predictions, ensure_ascii=False),
            _json.dumps(last_race, ensure_ascii=False) if last_race else None,
        ))


def get_athlete_cache(user_id: int) -> dict | None:
    """Возвращает кэш или None если нет / устарел (>30 дней)"""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT ctl, atl, tsb, trend_text, form_text, 
                   predictions, last_race, updated_at
            FROM athlete_cache WHERE user_id = ?
        """, (user_id,)).fetchone()

    if not row:
        return None

    # Проверяем свежесть
    try:
        updated = datetime.fromisoformat(row[7])
        if (datetime.now() - updated).days > 30:
            return None  # устарел
    except Exception:
        return None

    return {
        "training_load": {
            "ctl": row[0], "atl": row[1], "tsb": row[2],
            "trend_text": row[3], "form_text": row[4],
            "summary": (
                f"Тренированность (CTL): {row[0]}, "
                f"Усталость (ATL): {row[1]}, "
                f"Форма (TSB): {row[2]} — {row[4]}. "
                f"Тренд: {row[3]}."
            )
        },
        "predictions": _json.loads(row[5]) if row[5] else {},
        "last_race": _json.loads(row[6]) if row[6] else None,
        "updated_at": row[7],
    }


def save_pace_zones(user_id: int, zones: dict, source: str) -> None:
    """Сохраняет персональные темповые зоны в athlete_cache (upsert).
    Не затрагивает training_load (ctl/atl/tsb) — обновляет только поля зон.
    """
    payload = _json.dumps(zones, ensure_ascii=False)
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO athlete_cache (user_id, pace_zones_json, zones_source, zones_updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                pace_zones_json = excluded.pace_zones_json,
                zones_source = excluded.zones_source,
                zones_updated_at = excluded.zones_updated_at
        """, (user_id, payload, source))


def get_pace_zones_raw(user_id: int) -> dict | None:
    """Читает зоны из athlete_cache напрямую (не зависит от свежести training_load).
    Возвращает {"zones": dict, "source": str, "updated_at": str} или None.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT pace_zones_json, zones_source, zones_updated_at "
            "FROM athlete_cache WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        zones = _json.loads(row[0])
    except Exception:
        return None
    return {"zones": zones, "source": row[1], "updated_at": row[2]}


# ── Профиль спортсмена ────────────────────────────────────────

def save_user_profile(user_id: int, vo2max: float | None = None,
                      lactate_threshold_pace: str | None = None,
                      lactate_threshold_hr: int | None = None,
                      gender: str | None = None,
                      garmin_email: str | None = None,
                      garmin_password: str | None = None,
                      vo2max_source: str | None = None,
                      lactate_source: str | None = None,
                      coros_email: str | None = None,
                      coros_password: str | None = None,
                      polar_user_id: str | None = None,
                      specialization: str | None = None,
                      vo2max_locked: int | None = None,
                      lactate_locked: int | None = None,
                      birthdate: str | None = None,
                      coros_region: str | None = None):
    """Сохраняет или обновляет профиль спортсмена. None-поля не перезаписывают существующие."""
    from datetime import datetime as _dt
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT vo2max, lactate_threshold_pace, lactate_threshold_hr, gender, "
            "garmin_email, garmin_password, vo2max_source, vo2max_updated_at, lactate_source, "
            "coros_email, coros_password, polar_user_id, specialization, "
            "vo2max_locked, lactate_locked, birthdate, coros_region "
            "FROM user_profile WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if existing:
            new_vo2max = vo2max if vo2max is not None else existing[0]
            new_pace = lactate_threshold_pace if lactate_threshold_pace is not None else existing[1]
            new_hr = lactate_threshold_hr if lactate_threshold_hr is not None else existing[2]
            new_gender = gender if gender is not None else existing[3]
            new_garmin_email = _encrypt(garmin_email) if garmin_email is not None else existing[4]
            new_garmin_password = _encrypt(garmin_password) if garmin_password is not None else existing[5]
            new_vo2max_source = vo2max_source if vo2max_source is not None else existing[6]
            new_vo2max_updated_at = now if vo2max is not None else (existing[7] or now)
            new_lactate_source = lactate_source if lactate_source is not None else existing[8]
            new_coros_email = _encrypt(coros_email) if coros_email is not None else existing[9]
            new_coros_password = _encrypt(coros_password) if coros_password is not None else existing[10]
            new_polar_user_id = polar_user_id if polar_user_id is not None else (existing[11] if len(existing) > 11 else None)
            new_specialization = specialization if specialization is not None else (existing[12] if len(existing) > 12 else None)
            new_vo2max_locked = vo2max_locked if vo2max_locked is not None else (existing[13] if len(existing) > 13 else 0)
            new_lactate_locked = lactate_locked if lactate_locked is not None else (existing[14] if len(existing) > 14 else 0)
            new_birthdate = birthdate if birthdate is not None else (existing[15] if len(existing) > 15 else None)
            new_coros_region = coros_region if coros_region is not None else (existing[16] if len(existing) > 16 else None)
            conn.execute("""
                UPDATE user_profile
                SET vo2max = ?, lactate_threshold_pace = ?, lactate_threshold_hr = ?,
                    gender = ?, garmin_email = ?, garmin_password = ?,
                    vo2max_source = ?, vo2max_updated_at = ?, lactate_source = ?,
                    coros_email = ?, coros_password = ?, polar_user_id = ?,
                    specialization = ?, vo2max_locked = ?, lactate_locked = ?,
                    birthdate = ?, coros_region = ?,
                    updated_at = datetime('now')
                WHERE user_id = ?
            """, (new_vo2max, new_pace, new_hr, new_gender, new_garmin_email, new_garmin_password,
                  new_vo2max_source, new_vo2max_updated_at, new_lactate_source,
                  new_coros_email, new_coros_password, new_polar_user_id, new_specialization,
                  new_vo2max_locked, new_lactate_locked, new_birthdate, new_coros_region, user_id))
        else:
            conn.execute("""
                INSERT INTO user_profile (user_id, vo2max, lactate_threshold_pace, lactate_threshold_hr,
                    gender, garmin_email, garmin_password, vo2max_source, vo2max_updated_at, lactate_source,
                    coros_email, coros_password, polar_user_id, specialization,
                    vo2max_locked, lactate_locked, birthdate, coros_region)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, vo2max, lactate_threshold_pace, lactate_threshold_hr, gender,
                  _encrypt(garmin_email), _encrypt(garmin_password), vo2max_source,
                  now if vo2max is not None else None, lactate_source,
                  _encrypt(coros_email), _encrypt(coros_password), polar_user_id, specialization,
                  0, 0, birthdate, coros_region))



def get_user_profile(user_id: int) -> dict | None:
    """Профиль спортсмена с VO2max v2: поля vo2max/vo2max_source/vo2max_updated_at
    берутся из get_vo2max_resolved (device/manual + приоритет) — единая точка
    чтения для всех потребителей (промт, зоны, экран профиля, /report).
    Полный resolved-словарь доступен в ключе vo2max_resolved."""
    prof = _get_user_profile_row(user_id)
    if not prof:
        return None
    try:
        res = get_vo2max_resolved(user_id)
    except Exception:
        res = None
    if res:
        prof["vo2max"] = res["value"]
        prof["vo2max_source"] = ("manual" if res["kind"] == "manual"
                                 else (res["device"].get("source") or "auto"))
        prof["vo2max_updated_at"] = res["at"]
        prof["vo2max_resolved"] = res
    try:
        lres = get_lt_resolved(user_id)
    except Exception:
        lres = None
    if lres:
        prof["lactate_threshold_pace"] = lres["pace"]
        prof["lactate_threshold_hr"] = lres["hr"]
        prof["lactate_source"] = ("manual" if lres["kind"] == "manual"
                                  else (lres["device"].get("source") or "auto"))
        prof["lt_resolved"] = lres
    return prof


def _get_user_profile_row(user_id: int) -> dict | None:
    """Возвращает профиль спортсмена или None если не заполнен."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT vo2max, lactate_threshold_pace, lactate_threshold_hr, gender, updated_at,
                   garmin_email, garmin_password, vo2max_source, vo2max_updated_at, lactate_source,
                   coros_email, coros_password, polar_user_id, specialization,
                   vo2max_locked, lactate_locked, birthdate, coros_region
            FROM user_profile WHERE user_id = ?
        """, (user_id,)).fetchone()

    if not row:
        return None

    return {
        "vo2max": row[0],
        "lactate_threshold_pace": row[1],
        "lactate_threshold_hr": row[2],
        "gender": row[3],
        "updated_at": row[4],
        "garmin_email": _decrypt(row[5]),
        "garmin_password": _decrypt(row[6]),
        "vo2max_source": row[7],
        "vo2max_updated_at": row[8],
        "lactate_source": row[9],
        "coros_email": _decrypt(row[10]) if len(row) > 10 else None,
        "coros_password": _decrypt(row[11]) if len(row) > 11 else None,
        "polar_user_id": row[12] if len(row) > 12 else None,
        "specialization": row[13] if len(row) > 13 else None,
        "vo2max_locked": bool(row[14]) if len(row) > 14 else False,
        "lactate_locked": bool(row[15]) if len(row) > 15 else False,
        "birthdate": row[16] if len(row) > 16 else None,
        "coros_region": row[17] if len(row) > 17 else None,
    }


# ── Последняя рекомендация ────────────────────────────────────

def save_last_recommendation(
    user_id: int, advice: dict, workout: dict, ai_mode: str = "",
    evening_recovery_score: int | None = None,
    lowered_by_recovery: bool = False,
):
    """Сохраняет вечернюю рекомендацию для использования утром."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO last_recommendation
                (user_id, recommended_group, recommended_pace, reason,
                 if_feeling_good, if_tired, workout_date, workout_title,
                 groups_raw, extra_groups_raw, ai_mode,
                 evening_recovery_score, lowered_by_recovery, saved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                recommended_group = excluded.recommended_group,
                recommended_pace = excluded.recommended_pace,
                reason = excluded.reason,
                if_feeling_good = excluded.if_feeling_good,
                if_tired = excluded.if_tired,
                workout_date = excluded.workout_date,
                workout_title = excluded.workout_title,
                groups_raw = excluded.groups_raw,
                extra_groups_raw = excluded.extra_groups_raw,
                ai_mode = excluded.ai_mode,
                evening_recovery_score = excluded.evening_recovery_score,
                lowered_by_recovery = excluded.lowered_by_recovery,
                saved_at = excluded.saved_at
        """, (
            user_id,
            str(advice.get("recommended_group", "")),
            str(advice.get("recommended_pace", "")),
            str(advice.get("reason", "")),
            str(advice.get("if_feeling_good", "")),
            str(advice.get("if_tired", "")),
            workout.get("workout_date", ""),
            workout.get("location", ""),
            workout.get("groups_raw", ""),
            _json.dumps(workout.get("extra_groups_raw", []), ensure_ascii=False),
            ai_mode,
            int(evening_recovery_score) if evening_recovery_score is not None else None,
            1 if lowered_by_recovery else 0,
        ))


def get_last_recommendation(user_id: int, workout_date: str | None = None) -> dict | None:
    """Возвращает сохранённую вечернюю рекомендацию или None.
    Если workout_date передан — возвращает только рекомендацию для этой тренировки."""
    with get_connection() as conn:
        if workout_date:
            row = conn.execute("""
                SELECT recommended_group, recommended_pace, reason,
                       if_feeling_good, if_tired, workout_date, workout_title,
                       groups_raw, extra_groups_raw, saved_at,
                       ai_mode, evening_recovery_score, lowered_by_recovery
                FROM last_recommendation WHERE user_id = ? AND workout_date = ?
            """, (user_id, workout_date)).fetchone()
        else:
            row = conn.execute("""
                SELECT recommended_group, recommended_pace, reason,
                       if_feeling_good, if_tired, workout_date, workout_title,
                       groups_raw, extra_groups_raw, saved_at,
                       ai_mode, evening_recovery_score, lowered_by_recovery
                FROM last_recommendation WHERE user_id = ?
            """, (user_id,)).fetchone()

    if not row:
        return None

    # Рекомендация устаревает через 36 часов
    try:
        saved = datetime.fromisoformat(row[9])
        if (datetime.now() - saved).total_seconds() > 36 * 3600:
            return None
    except Exception:
        return None

    return {
        "recommended_group": row[0],
        "recommended_pace": row[1],
        "reason": row[2],
        "if_feeling_good": row[3],
        "if_tired": row[4],
        "workout_date": row[5],
        "workout_title": row[6],
        "groups_raw": row[7],
        "extra_groups_raw": _json.loads(row[8]) if row[8] else [],
        "saved_at": row[9],
        "ai_mode": row[10],
        "evening_recovery_score": row[11],
        "lowered_by_recovery": bool(row[12]) if row[12] is not None else False,
    }


def get_recommendations_for_date(workout_date: str) -> list[dict]:
    """Все рекомендации за указанную дату (для отчёта админу после рассылки).
    JOIN с users по имени. last_recommendation — одна строка на юзера,
    поэтому это ровно те, кому ушла рекомендация на эту тренировку.
    Возвращает список {name, recommended_group, evening_recovery_score, lowered_by_recovery}."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT COALESCE(u.name, u.username, 'user_' || lr.user_id),
                   lr.recommended_group, lr.evening_recovery_score, lr.lowered_by_recovery,
                   u.username, u.telegram_id
            FROM last_recommendation lr
            JOIN users u ON u.id = lr.user_id
            WHERE lr.workout_date = ?
            ORDER BY lr.recommended_group, COALESCE(u.name, u.username)
        """, (workout_date,)).fetchall()
    return [
        {
            "name": r[0],
            "recommended_group": r[1],
            "evening_recovery_score": r[2],
            "lowered_by_recovery": bool(r[3]) if r[3] is not None else False,
            "username": r[4],
            "telegram_id": r[5],
        }
        for r in rows
    ]


# ── Кэш Garmin recovery ──────────────────────────────────────

def get_garmin_recovery_cache(user_id: int, max_age_hours: int | None = None) -> dict | None:
    """Кэш Garmin-восстановления. 17.08.2026 (Антон): по умолчанию живёт
    ДО КОНЦА ДНЯ (МСК), а не фиксированное окно часов; max_age_hours — опция
    для вызовов, которым нужно жёсткое окно."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT body_battery, hrv_last_night, hrv_weekly_avg, hrv_status,
                   tr_score, tr_level, tr_factors, updated_at
            FROM garmin_recovery_cache WHERE user_id = ?
        """, (user_id,)).fetchone()
    if not row:
        return None
    try:
        upd = datetime.fromisoformat(row[7])
        if max_age_hours is not None:
            if (datetime.now() - upd).total_seconds() / 3600 > max_age_hours:
                return None
        else:
            from datetime import timedelta as _td
            # updated_at пишется datetime('now') = UTC; сравниваем даты в МСК (+3)
            if (upd + _td(hours=3)).date() != (datetime.utcnow() + _td(hours=3)).date():
                return None
    except Exception:
        return None
    return {
        "source": "garmin",
        "body_battery": row[0],
        "hrv": row[1],
        "hrv_weekly_avg": row[2],
        "hrv_status": row[3],
        "training_readiness": {
            "score": row[4],
            "level": row[5],
            "factors": _json.loads(row[6]) if row[6] else [],
        } if row[4] is not None else None,
        "updated_at": row[7],
    }


def save_garmin_recovery_cache(user_id: int, data: dict):
    tr = data.get("training_readiness") or {}
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO garmin_recovery_cache
                (user_id, body_battery, hrv_last_night, hrv_weekly_avg, hrv_status,
                 tr_score, tr_level, tr_factors, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                body_battery = excluded.body_battery,
                hrv_last_night = excluded.hrv_last_night,
                hrv_weekly_avg = excluded.hrv_weekly_avg,
                hrv_status = excluded.hrv_status,
                tr_score = excluded.tr_score,
                tr_level = excluded.tr_level,
                tr_factors = excluded.tr_factors,
                updated_at = excluded.updated_at
        """, (
            user_id,
            data.get("body_battery"),
            data.get("hrv"),
            data.get("hrv_weekly_avg"),
            data.get("hrv_status"),
            tr.get("score"),
            tr.get("level"),
            _json.dumps(tr.get("factors", []), ensure_ascii=False),
        ))


# ── Уведомления об анонсах ────────────────────────────────────

def get_workout_notification(post_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT post_id, post_type, workout_date, notified_at, notified_extra_groups, users_notified "
            "FROM workout_notifications WHERE post_id = ?", (post_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "post_id": row[0],
        "post_type": row[1] or "interval",
        "workout_date": row[2],
        "notified_at": row[3],
        "notified_extra_groups": _json.loads(row[4]) if row[4] else [],
        "users_notified": row[5],
    }


def save_workout_notification(post_id: int, post_type: str, workout_date: str,
                               notified_extra_groups: list, users_notified: int):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO workout_notifications
                (post_id, post_type, workout_date, notified_at, notified_extra_groups, users_notified)
            VALUES (?, ?, ?, datetime('now'), ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                notified_extra_groups = excluded.notified_extra_groups,
                users_notified = users_notified + excluded.users_notified
        """, (post_id, post_type, workout_date,
              _json.dumps(notified_extra_groups, ensure_ascii=False), users_notified))


def get_last_workout_notification() -> dict | None:
    """Последний анонс для отображения в /status."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT post_id, post_type, workout_date, notified_at, users_notified "
            "FROM workout_notifications ORDER BY notified_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return {
        "post_id": row[0],
        "post_type": row[1],
        "workout_date": row[2],
        "notified_at": row[3],
        "users_notified": row[4],
    }


# ── Обратная связь ────────────────────────────────────────────

def save_feedback(user_id: int, feedback_type: str, text: str) -> None:
    """Сохраняет сообщение обратной связи (bug / feature)."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO feedback (user_id, type, text) VALUES (?, ?, ?)",
            (user_id, feedback_type, text)
        )


def get_recent_feedbacks(limit: int = 20) -> list:
    """Последние N сообщений обратной связи с данными пользователя."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT f.id, f.type, f.text, f.created_at, u.name, u.username
            FROM feedback f
            JOIN users u ON f.user_id = u.id
            ORDER BY f.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()


# ── Оценки рекомендаций ───────────────────────────────────────

def save_rating(user_id: int, workout_date: str, rating: int,
                ai_mode: str, comment: str = None) -> None:
    """Сохраняет оценку рекомендации."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO recommendation_ratings (user_id, workout_date, rating, ai_mode, comment) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, workout_date, rating, ai_mode, comment)
        )


def get_recent_ratings(limit: int = 20) -> list:
    """Последние N оценок с данными пользователя."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT r.id, r.rating, r.ai_mode, r.comment, r.created_at, r.workout_date,
                   u.name, u.username
            FROM recommendation_ratings r
            JOIN users u ON r.user_id = u.id
            ORDER BY r.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()


# ── VO2max v2: device/manual + приоритет ────────────────────
# Новая схема хранения (пока никем не вызывается, боевые ветки не тронуты):
# значение из систем и ручное живут в разных полях, приоритет решает чтение.

def save_vo2max_device(user_id: int, value: float, source: str) -> None:
    """Запись VO2max из трекера (garmin/coros/polar). Всегда, без порогов и lock —
    приоритет решается при чтении, а не при записи."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE user_profile SET vo2max_device = ?, vo2max_device_source = ?, "
            "vo2max_device_at = ? WHERE user_id = ?",
            (float(value), source, now, user_id))


def save_vo2max_manual(user_id: int, value: float) -> None:
    """Запись ручного VO2max (кнопка в профиле). Ставит приоритет manual —
    явный ввод означает «используй моё», пока пользователь не переключит."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE user_profile SET vo2max_manual = ?, vo2max_manual_at = ?, "
            "vo2max_priority = 'manual' WHERE user_id = ?",
            (float(value), now, user_id))


def set_vo2max_priority(user_id: int, priority: str) -> None:
    """Переключатель 'manual' | 'device' (будущая замена замка 🔒)."""
    with get_connection() as conn:
        conn.execute("UPDATE user_profile SET vo2max_priority = ? WHERE user_id = ?",
                     (priority, user_id))


def get_vo2max_resolved(user_id: int) -> dict | None:
    """Единая точка чтения VO2max.
    Приоритет из vo2max_priority (дефолт device); если выбранного значения нет —
    фолбэк на второе (только ручное без трекера — штатный случай).
    Возвращает {value, source, kind, at, age_days, device, manual, priority}
    или None, если нет ни одного значения. Свежесть (age_days) интерпретирует
    вызывающий."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT vo2max_device, vo2max_device_source, vo2max_device_at, "
            "vo2max_manual, vo2max_manual_at, vo2max_priority "
            "FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    dev_v, dev_src, dev_at, man_v, man_at, prio = row

    def _age(ts):
        if not ts:
            return None
        try:
            return (datetime.now() - datetime.fromisoformat(ts)).days
        except Exception:
            return None

    device = {"value": dev_v, "source": dev_src, "at": dev_at, "age_days": _age(dev_at)}
    manual = {"value": man_v, "at": man_at, "age_days": _age(man_at)}
    prio = prio or "device"
    order = ("manual", "device") if prio == "manual" else ("device", "manual")
    for kind in order:
        v = man_v if kind == "manual" else dev_v
        if v is not None:
            return {"value": float(v),
                    "source": "вручную" if kind == "manual" else (dev_src or "трекер"),
                    "kind": kind,
                    "at": man_at if kind == "manual" else dev_at,
                    "age_days": manual["age_days"] if kind == "manual" else device["age_days"],
                    "device": device, "manual": manual, "priority": prio}
    return None


# ── Лактатный порог v2: device/manual + приоритет (зеркало VO2max v2) ──

def save_lt_device(user_id: int, pace: str, hr, source: str) -> None:
    """Запись ЛП из трекера (темп обязателен, пульс может быть None).
    Всегда, без порогов и lock — приоритет решается при чтении."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE user_profile SET lt_pace_device = ?, lt_hr_device = ?, "
            "lt_device_source = ?, lt_device_at = ? WHERE user_id = ?",
            (str(pace), int(hr) if hr is not None else None, source, now, user_id))


def save_lt_manual(user_id: int, pace, hr) -> None:
    """Запись ручного ЛП. Ставит приоритет manual (явный ввод = «используй моё»).
    Ввод в боте двухшаговый (темп, потом пульс), поэтому None не затирает
    вторую половину — COALESCE оставляет прежнее значение."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE user_profile SET "
            "lt_pace_manual = COALESCE(?, lt_pace_manual), "
            "lt_hr_manual = COALESCE(?, lt_hr_manual), "
            "lt_manual_at = ?, lt_priority = 'manual' WHERE user_id = ?",
            (str(pace) if pace is not None else None,
             int(hr) if hr is not None else None, now, user_id))


def set_lt_priority(user_id: int, priority: str) -> None:
    """Переключатель 'manual' | 'device' для ЛП."""
    with get_connection() as conn:
        conn.execute("UPDATE user_profile SET lt_priority = ? WHERE user_id = ?",
                     (priority, user_id))


def get_lt_resolved(user_id: int) -> dict | None:
    """Единая точка чтения лактатного порога (pace+hr).
    Приоритет из lt_priority (дефолт device); фолбэк на второе значение.
    Значение считается заданным, если есть темп (пульс опционален).
    Возвращает {pace, hr, source, kind, at, age_days, device, manual, priority} или None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT lt_pace_device, lt_hr_device, lt_device_source, lt_device_at, "
            "lt_pace_manual, lt_hr_manual, lt_manual_at, lt_priority "
            "FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    dp, dh, dsrc, dat, mp, mh, mat, prio = row

    def _age(ts):
        if not ts:
            return None
        try:
            return (datetime.now() - datetime.fromisoformat(ts)).days
        except Exception:
            return None

    device = {"pace": dp, "hr": dh, "source": dsrc, "at": dat, "age_days": _age(dat)}
    manual = {"pace": mp, "hr": mh, "at": mat, "age_days": _age(mat)}
    prio = prio or "device"
    order = ("manual", "device") if prio == "manual" else ("device", "manual")
    for kind in order:
        pace = mp if kind == "manual" else dp
        if pace:
            return {"pace": pace,
                    "hr": mh if kind == "manual" else dh,
                    "source": "вручную" if kind == "manual" else (dsrc or "трекер"),
                    "kind": kind,
                    "at": mat if kind == "manual" else dat,
                    "age_days": manual["age_days"] if kind == "manual" else device["age_days"],
                    "device": device, "manual": manual, "priority": prio}
    return None


# ── workout_analysis ──────────────────────────────────────────

import logging as _logging
_db_logger = _logging.getLogger(__name__)


def save_workout_analysis(
    post_id: int,
    workout_date: str,
    workout_type: str,
    is_valid: int,
    raw_text: str,
    analyzed_json: str,
    analysis_mode: str,
    extra_groups_json: str | None = None,
    edit_date: str | None = None,
) -> None:
    """Сохраняет или обновляет анализ тренировки.
    extra_groups_json / edit_date — опциональны; при None существующие НЕ затираются (COALESCE).
    """
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO workout_analysis
                (post_id, workout_date, workout_type, is_valid, raw_text,
                 analyzed_json, extra_groups_json, analysis_mode, created_at, updated_at, edit_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                workout_date      = excluded.workout_date,
                workout_type      = excluded.workout_type,
                is_valid          = excluded.is_valid,
                raw_text          = excluded.raw_text,
                analyzed_json     = excluded.analyzed_json,
                extra_groups_json = COALESCE(excluded.extra_groups_json, workout_analysis.extra_groups_json),
                analysis_mode     = excluded.analysis_mode,
                updated_at        = excluded.updated_at,
                edit_date         = COALESCE(excluded.edit_date, workout_analysis.edit_date)
        """, (post_id, workout_date, workout_type, is_valid, raw_text,
              analyzed_json, extra_groups_json, analysis_mode, now, now, edit_date))
    _db_logger.info(
        f"Анализ тренировки сохранён: post_id={post_id}, "
        f"type={workout_type}, valid={is_valid}, mode={analysis_mode}"
    )


def get_workout_analysis(post_id: int) -> dict | None:
    """Возвращает анализ по post_id или None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM workout_analysis WHERE post_id = ?", (post_id,)
        ).fetchone()
    if not row:
        return None
    cols = ["id", "post_id", "workout_date", "workout_type", "is_valid",
            "raw_text", "analyzed_json", "extra_groups_json",
            "analysis_mode", "created_at", "updated_at", "edit_date"]
    return dict(zip(cols, row))


def get_latest_workout_analysis(
    workout_type: str,
    current_post_id: int | None = None,
    current_workout_date: str | None = None,
    current_edit_date: str | None = None,
) -> tuple[dict | None, str]:
    """Возвращает (analysis | None, status) для двухшагового флоу.

    status:
      'current'   — есть валидный анализ под текущий анонс (workout_date >= сегодня)
      'analyzing' — в канале анонс новее кэша (другой post_id / правка) — отдаём прошлый анализ
      'past'      — будущего/сегодняшнего анонса нет, только прошедший
      'empty'     — анализа нет вообще

    current_* — что сейчас в канале (из find_next_*); сравнение по post_id/edit_date,
    без тяжёлого DeepSeek-анализа. Если current_post_id=None — статус по дате анализа.
    """
    from datetime import date as _date
    today = _date.today().isoformat()

    with get_connection() as conn:
        row = conn.execute("""
            SELECT * FROM workout_analysis
            WHERE workout_type = ? AND is_valid = 1
            ORDER BY workout_date DESC, created_at DESC
            LIMIT 1
        """, (workout_type,)).fetchone()
    if not row:
        return None, "empty"
    cols = ["id", "post_id", "workout_date", "workout_type", "is_valid",
            "raw_text", "analyzed_json", "extra_groups_json",
            "analysis_mode", "created_at", "updated_at", "edit_date"]
    analysis = dict(zip(cols, row))

    # Свежий анонс в канале новее кэша → кэш ещё в проработке
    if current_post_id is not None:
        cache_stale = (analysis.get("post_id") != current_post_id)
        if not cache_stale and current_edit_date:
            old_ed = analysis.get("edit_date")
            if not old_ed or str(current_edit_date) > str(old_ed):
                cache_stale = True
        if cache_stale:
            return analysis, "analyzing"

    # Кэш актуален (или анонса в канале нет) → current vs past по дате
    if (analysis.get("workout_date") or "") >= today:
        return analysis, "current"
    return analysis, "past"


def update_extra_groups(post_id: int, extra_groups_json: str) -> None:
    """Обновляет extra_groups_json для существующего анализа."""
    with get_connection() as conn:
        conn.execute("""
            UPDATE workout_analysis
            SET extra_groups_json = ?, updated_at = datetime('now')
            WHERE post_id = ?
        """, (extra_groups_json, post_id))
    _db_logger.info(f"extra_groups обновлены: post_id={post_id}")


# ── Глобальные настройки бота ─────────────────────────────────

def save_workout_template(workout_date: str, group_number: str,
                          wtype: str, workout_json: str) -> None:
    """Сохраняет эталон тренировки (готовый Garmin JSON) для группы.

    workout_json — строка JSON (уже сериализованный воркаут). upsert по (workout_date, group_number, wtype).
    """
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO workout_templates
                (workout_date, group_number, wtype, workout_json, created_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(workout_date, group_number, wtype) DO UPDATE SET
                workout_json = excluded.workout_json,
                created_at = excluded.created_at
        """, (workout_date, group_number, wtype, workout_json))
    _db_logger.info(
        f"workout_template сохранён: date={workout_date} group={group_number} type={wtype}"
    )


def get_workout_template(workout_date: str, group_number: str,
                         wtype: str = "interval") -> str | None:
    """Читает сохранённый эталон (Garmin JSON-строку) по (workout_date, group_number, wtype).
    Возвращает строку workout_json или None, если эталона нет."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT workout_json FROM workout_templates "
            "WHERE workout_date = ? AND group_number = ? AND wtype = ?",
            (workout_date, group_number, wtype)
        ).fetchone()
    return row[0] if row else None


def get_preprocess_mode() -> str:
    """Режим анализа тренировок (deep/smart). Глобальная настройка."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = 'preprocess_mode'"
        ).fetchone()
    return row[0] if row else "deep"


def set_preprocess_mode(mode: str) -> None:
    """Устанавливает режим анализа тренировок (deep/smart)."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('preprocess_mode', ?)",
            (mode,)
        )
    _db_logger.info(f"preprocess_mode установлен: {mode}")


def save_raw_service_data(user_id: int, service: str, raw_json: str) -> None:
    """Слой 1.1: сохраняет сырой ответ сервиса as is.

    raw_json — строка JSON с сырыми данными от API сервиса, без обработки.
    """
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO raw_service_data (user_id, service, raw_json, fetched_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (user_id, service, raw_json)
        )
    _db_logger.info(f"raw_service_data сохранён: user={user_id} service={service}")


def get_raw_service_data(user_id: int, service: str) -> dict | None:
    """Читает сырые данные сервиса. Возвращает {raw_json, fetched_at} или None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT raw_json, fetched_at FROM raw_service_data WHERE user_id = ? AND service = ?",
            (user_id, service)
        ).fetchone()
    if not row:
        return None
    return {"raw_json": row[0], "fetched_at": row[1]}


# ── Нормализованный кэш (слой 2) ─────────────────────────────

def save_unified_data(user_id: int, unified_json: str, sources: str = "") -> None:
    """Слой 2: сохраняет нормализованные данные всех сервисов в unified_cache.

    ВАЖНО: upsert через ON CONFLICT обновляет ТОЛЬКО unified_json/sources/updated_at.
    Колонки снимка "на утро" (morning_*) НЕ трогаются — иначе перенормализация
    затирала бы пойманный утренний снимок (был баг с INSERT OR REPLACE).
    """
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO unified_cache (user_id, unified_json, sources, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                unified_json = excluded.unified_json,
                sources = excluded.sources,
                updated_at = excluded.updated_at
        """, (user_id, unified_json, sources))
    _db_logger.info(f"unified_cache сохранён: user={user_id} sources={sources}")


def get_unified_data(user_id: int, max_age_hours: int = 12) -> dict | None:
    """Слой 2: читает нормализованные данные из unified_cache.

    Возвращает {unified_json, sources, updated_at} или None если нет/устарел.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT unified_json, sources, updated_at FROM unified_cache WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    if not row:
        return None
    try:
        age = (datetime.now() - datetime.fromisoformat(row[2])).total_seconds() / 3600
        if age > max_age_hours:
            return None
    except Exception:
        return None
    return {"unified_json": row[0], "sources": row[1], "updated_at": row[2]}


def set_morning_caught(user_id: int, date_msk: str, snapshot: dict | None = None) -> None:
    """Слой 3 (детектор пробуждения): помечает, что ночь поймана за date_msk,
    и замораживает снимок "на утро".

    snapshot — dict с ключами: tr, bb, hrv, rhr, sleep_h, wake_at, snapshot_at (любые могут отсутствовать).
    Пишет в unified_cache. Строка юзера уже должна существовать (создаётся нормализацией);
    если её нет — создаёт минимальную запись.
    """
    s = snapshot or {}
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE unified_cache SET morning_caught = 1, morning_date = ?, "
            "morning_tr = ?, morning_bb = ?, morning_hrv = ?, morning_rhr = ?, "
            "morning_sleep_h = ?, morning_wake_at = ?, morning_snapshot_at = ? "
            "WHERE user_id = ?",
            (date_msk, s.get("tr"), s.get("bb"), s.get("hrv"), s.get("rhr"),
             s.get("sleep_h"), s.get("wake_at"), s.get("snapshot_at"), user_id)
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO unified_cache (user_id, unified_json, sources, updated_at, "
                "morning_caught, morning_date, morning_tr, morning_bb, morning_hrv, "
                "morning_rhr, morning_sleep_h, morning_wake_at, morning_snapshot_at) "
                "VALUES (?, '', '', datetime('now'), 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, date_msk, s.get("tr"), s.get("bb"), s.get("hrv"),
                 s.get("rhr"), s.get("sleep_h"), s.get("wake_at"), s.get("snapshot_at"))
            )
        # История слепков: пишем ту же запись в mornings сразу после unified_cache.
        # sources берём из текущей строки unified_cache юзера. upsert по (user_id, date).
        _src_row = conn.execute(
            "SELECT sources FROM unified_cache WHERE user_id = ?", (user_id,)
        ).fetchone()
        _sources = _src_row[0] if _src_row else None
        conn.execute(
            "INSERT INTO mornings (user_id, date, sources, morning_caught, "
            "morning_tr, morning_bb, morning_hrv, morning_rhr, morning_sleep_h, "
            "morning_wake_at, morning_snapshot_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, date) DO UPDATE SET "
            "sources = excluded.sources, morning_caught = excluded.morning_caught, "
            "morning_tr = excluded.morning_tr, morning_bb = excluded.morning_bb, "
            "morning_hrv = excluded.morning_hrv, morning_rhr = excluded.morning_rhr, "
            "morning_sleep_h = excluded.morning_sleep_h, morning_wake_at = excluded.morning_wake_at, "
            "morning_snapshot_at = excluded.morning_snapshot_at",
            (user_id, date_msk, _sources, s.get("tr"), s.get("bb"), s.get("hrv"),
             s.get("rhr"), s.get("sleep_h"), s.get("wake_at"), s.get("snapshot_at"))
        )
    _db_logger.info(f"morning_caught: user={user_id} date={date_msk} snap={bool(snapshot)}")


def get_morning_caught(user_id: int) -> dict | None:
    """Слой 3: читает флаг пойманной ночи и снимок на утро.
    Возвращает {caught, date, tr, bb, hrv, rhr, sleep_h, wake_at, snapshot_at} или None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT morning_caught, morning_date, morning_tr, morning_bb, morning_hrv, "
            "morning_rhr, morning_sleep_h, morning_wake_at, morning_snapshot_at "
            "FROM unified_cache WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "caught": bool(row[0]), "date": row[1],
        "tr": row[2], "bb": row[3], "hrv": row[4], "rhr": row[5],
        "sleep_h": row[6], "wake_at": row[7], "snapshot_at": row[8],
    }


def get_users_list_for_b() -> list[dict]:
    """Пользователи для кнопок /b и /a_user."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, telegram_id, COALESCE(name, username, 'user_' || id), username "
            "FROM users ORDER BY id"
        ).fetchall()
    return [{"db_user_id": r[0], "telegram_id": r[1], "name": r[2], "username": r[3]} for r in rows]


if __name__ == "__main__":
    # Тест: запусти python src/database.py чтобы проверить
    from dotenv import load_dotenv
    load_dotenv()
    init_db()
    uid = get_or_create_user(123456789, "Тест Пользователь", "testuser")
    print(f"User ID: {uid}")
    save_token(uid, "strava", "test_access_token_123", "test_refresh_token_456")
    token = get_token(uid, "strava")
    print(f"Token: {token}")
    print("✅ Всё работает!")