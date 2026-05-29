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
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS user_profile (
                user_id INTEGER PRIMARY KEY,
                vo2max REAL,
                lactate_threshold_pace TEXT,
                lactate_threshold_hr INTEGER,
                gender TEXT,
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
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
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
                "notify_long INTEGER DEFAULT 1"):
        with get_connection() as conn:
            try:
                conn.execute(f"ALTER TABLE user_preferences ADD COLUMN {col}")
            except Exception:
                pass
    for col in ("garmin_email TEXT", "garmin_password TEXT",
                "vo2max_source TEXT", "vo2max_updated_at TEXT",
                "lactate_source TEXT",
                "coros_email TEXT", "coros_password TEXT",
                "polar_user_id TEXT"):
        with get_connection() as conn:
            try:
                conn.execute(f"ALTER TABLE user_profile ADD COLUMN {col}")
            except Exception:
                pass

    # Дефолт глобальной настройки режима анализа тренировок
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('preprocess_mode', 'deep')"
        )

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
            LEFT JOIN user_profiles pr   ON u.id = pr.user_id
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
                   is_active, deactivated_at
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
    }

def log_activity(user_id: int, command: str) -> None:
    """Логирует вызов команды пользователем."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_activity (user_id, command) VALUES (?, ?)",
            (user_id, command)
        )


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
        workout_7d = conn.execute(
            "SELECT COUNT(*) FROM user_activity "
            "WHERE command = '/workout' AND created_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
        long_7d = conn.execute(
            "SELECT COUNT(*) FROM user_activity "
            "WHERE command = '/long' AND created_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
        morning_7d = conn.execute(
            "SELECT COUNT(*) FROM user_activity "
            "WHERE command = '/morning' AND created_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
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
        "avg_rating": avg_rating, "ratings_30d": ratings_30d,
        "feedback_total": feedback_total, "feedback_bugs": feedback_bugs,
        "feedback_features": feedback_features,
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
                      polar_user_id: str | None = None):
    """Сохраняет или обновляет профиль спортсмена. None-поля не перезаписывают существующие."""
    from datetime import datetime as _dt
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT vo2max, lactate_threshold_pace, lactate_threshold_hr, gender, "
            "garmin_email, garmin_password, vo2max_source, vo2max_updated_at, lactate_source, "
            "coros_email, coros_password, polar_user_id "
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
            conn.execute("""
                UPDATE user_profile
                SET vo2max = ?, lactate_threshold_pace = ?, lactate_threshold_hr = ?,
                    gender = ?, garmin_email = ?, garmin_password = ?,
                    vo2max_source = ?, vo2max_updated_at = ?, lactate_source = ?,
                    coros_email = ?, coros_password = ?, polar_user_id = ?,
                    updated_at = datetime('now')
                WHERE user_id = ?
            """, (new_vo2max, new_pace, new_hr, new_gender, new_garmin_email, new_garmin_password,
                  new_vo2max_source, new_vo2max_updated_at, new_lactate_source,
                  new_coros_email, new_coros_password, new_polar_user_id, user_id))
        else:
            conn.execute("""
                INSERT INTO user_profile (user_id, vo2max, lactate_threshold_pace, lactate_threshold_hr,
                    gender, garmin_email, garmin_password, vo2max_source, vo2max_updated_at, lactate_source,
                    coros_email, coros_password, polar_user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, vo2max, lactate_threshold_pace, lactate_threshold_hr, gender,
                  _encrypt(garmin_email), _encrypt(garmin_password), vo2max_source,
                  now if vo2max is not None else None, lactate_source,
                  _encrypt(coros_email), _encrypt(coros_password), polar_user_id))



def get_user_profile(user_id: int) -> dict | None:
    """Возвращает профиль спортсмена или None если не заполнен."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT vo2max, lactate_threshold_pace, lactate_threshold_hr, gender, updated_at,
                   garmin_email, garmin_password, vo2max_source, vo2max_updated_at, lactate_source,
                   coros_email, coros_password, polar_user_id
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
    }


# ── Последняя рекомендация ────────────────────────────────────

def save_last_recommendation(user_id: int, advice: dict, workout: dict):
    """Сохраняет вечернюю рекомендацию для использования утром."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO last_recommendation
                (user_id, recommended_group, recommended_pace, reason,
                 if_feeling_good, if_tired, workout_date, workout_title,
                 groups_raw, extra_groups_raw, saved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
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
        ))


def get_last_recommendation(user_id: int, workout_date: str | None = None) -> dict | None:
    """Возвращает сохранённую вечернюю рекомендацию или None.
    Если workout_date передан — возвращает только рекомендацию для этой тренировки."""
    with get_connection() as conn:
        if workout_date:
            row = conn.execute("""
                SELECT recommended_group, recommended_pace, reason,
                       if_feeling_good, if_tired, workout_date, workout_title,
                       groups_raw, extra_groups_raw, saved_at
                FROM last_recommendation WHERE user_id = ? AND workout_date = ?
            """, (user_id, workout_date)).fetchone()
        else:
            row = conn.execute("""
                SELECT recommended_group, recommended_pace, reason,
                       if_feeling_good, if_tired, workout_date, workout_title,
                       groups_raw, extra_groups_raw, saved_at
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
    }


# ── Кэш Garmin recovery ──────────────────────────────────────

def get_garmin_recovery_cache(user_id: int, max_age_hours: int = 8) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT body_battery, hrv_last_night, hrv_weekly_avg, hrv_status,
                   tr_score, tr_level, tr_factors, updated_at
            FROM garmin_recovery_cache WHERE user_id = ?
        """, (user_id,)).fetchone()
    if not row:
        return None
    try:
        age_hours = (datetime.now() - datetime.fromisoformat(row[7])).total_seconds() / 3600
        if age_hours > max_age_hours:
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
) -> None:
    """Сохраняет или обновляет анализ тренировки."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO workout_analysis
                (post_id, workout_date, workout_type, is_valid, raw_text,
                 analyzed_json, analysis_mode, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                workout_date   = excluded.workout_date,
                workout_type   = excluded.workout_type,
                is_valid       = excluded.is_valid,
                raw_text       = excluded.raw_text,
                analyzed_json  = excluded.analyzed_json,
                analysis_mode  = excluded.analysis_mode,
                updated_at     = excluded.updated_at
        """, (post_id, workout_date, workout_type, is_valid, raw_text,
              analyzed_json, analysis_mode, now, now))
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
            "analysis_mode", "created_at", "updated_at"]
    return dict(zip(cols, row))


def get_latest_workout_analysis(workout_type: str) -> dict | None:
    """Последний валидный анализ нужного типа ('interval' или 'long')."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT * FROM workout_analysis
            WHERE workout_type = ? AND is_valid = 1
            ORDER BY workout_date DESC, created_at DESC
            LIMIT 1
        """, (workout_type,)).fetchone()
    if not row:
        return None
    cols = ["id", "post_id", "workout_date", "workout_type", "is_valid",
            "raw_text", "analyzed_json", "extra_groups_json",
            "analysis_mode", "created_at", "updated_at"]
    return dict(zip(cols, row))


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