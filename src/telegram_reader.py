import os
import re
from datetime import datetime, date
from telethon import TelegramClient
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
CHANNEL = "@Dusty_Dumbbells"
SESSION_FILE = os.path.join(os.path.dirname(__file__), '..', 'session')

# ID пользователей с расширенным доступом (admin mode)
ADMIN_TELEGRAM_IDS = {273726778}

EMOJI_NUMBERS = {
    '1️⃣': '1', '2️⃣': '2', '3️⃣': '3',
    '4️⃣': '4', '5️⃣': '5', '6️⃣': '6', '7️⃣': '7', '8️⃣': '8',
}

WEEKDAYS_RU = {
    'понедельник': 0, 'вторник': 1, 'среда': 2, 'среду': 2,
    'четверг': 3, 'пятница': 4, 'пятницу': 4,
    'суббота': 5, 'воскресенье': 6,
}

WORKOUT_KEYWORDS = [
    'разминка', 'группа', 'темп', 'мин/км', 'старт', 'сбор',
    'работа', 'заминка', 'объём', 'объем', 'км', 'ddlong'
]


def get_client() -> TelegramClient:
    return TelegramClient(SESSION_FILE, API_ID, API_HASH)


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_TELEGRAM_IDS


async def get_recent_posts(limit: int = 50) -> list:
    posts = []
    async with get_client() as client:
        async for message in client.iter_messages(CHANNEL, limit=limit):
            if message.text:
                posts.append({
                    "id": message.id,
                    "date": message.date,
                    "text": message.text
                })
    return posts


async def get_post_comments(post_id: int) -> list:
    """Получает комментарии к посту"""
    comments = []
    async with get_client() as client:
        try:
            async for message in client.iter_messages(
                CHANNEL,
                reply_to=post_id,
                limit=50
            ):
                if message.text:
                    comments.append({
                        "id": message.id,
                        "date": message.date,
                        "text": message.text,
                    })
        except Exception as e:
            print(f"Ошибка чтения комментариев: {e}")
    return comments


async def find_next_workout(only_interval: bool = True) -> dict | None:
    """
    Ищет ближайшую тренировку вт/пт в канале.
    Читает комментарии для поиска дополнительных групп.
    """
    today = date.today()
    posts = await get_recent_posts(limit=50)

    future_workouts = []
    past_workouts = []

    for post in posts:
        text_lower = post["text"].lower()
        matches = sum(1 for kw in WORKOUT_KEYWORDS if kw in text_lower)
        if matches < 3:
            continue

        parsed = parse_workout(post["text"], post["date"])
        if not parsed:
            continue

        # Читаем комментарии — ищем дополнительные группы
        comments = await get_post_comments(post["id"])
        extra_groups = []
        for comment in comments:
            group = parse_extra_group_from_comment(comment["text"])
            if group:
                extra_groups.append(group)

        if extra_groups:
            parsed["extra_groups"] = extra_groups
            parsed["extra_groups_raw"] = [c["text"] for c in comments
                                          if parse_extra_group_from_comment(c["text"])]

        try:
            workout_date = datetime.strptime(parsed["workout_date"], "%Y-%m-%d").date()
        except ValueError:
            continue

        is_interval = workout_date.weekday() in [1, 4]
        if only_interval and not is_interval:
            continue

        if workout_date >= today:
            parsed["is_past"] = False
            future_workouts.append(parsed)
        else:
            parsed["is_past"] = True
            past_workouts.append(parsed)

    if future_workouts:
        future_workouts.sort(key=lambda x: x["workout_date"])
        return future_workouts[0]

    if past_workouts:
        past_workouts.sort(key=lambda x: x["workout_date"], reverse=True)
        return past_workouts[0]

    return None


def parse_workout(text: str, post_date: datetime) -> dict | None:
    """
    Парсит пост тренировки.
    Вытаскивает: дату, локацию, расписание, работу и группы как сырой текст.
    """
    workout_date, weekday_name = _extract_date_and_weekday(text)
    if not workout_date:
        workout_date = post_date.strftime("%Y-%m-%d")

    workout_type = _get_workout_type(weekday_name)
    location = _extract_location(text)
    schedule = _extract_schedule(text)
    total_volume = _extract_volume(text)

    # Вытаскиваем блок "Работа" и блок "Группы" как сырой текст
    work_text = _extract_work_block(text)
    groups_raw = _extract_groups_block(text)

    if not groups_raw and workout_type not in ['long']:
        return None

    return {
        "post_date": post_date.strftime("%Y-%m-%d %H:%M"),
        "workout_date": workout_date,
        "weekday": weekday_name,
        "workout_type": workout_type,
        "location": location,
        "schedule": schedule,
        "work_text": work_text,        # сырой текст работы
        "groups_raw": groups_raw,      # сырой текст групп
        "total_volume_km": total_volume,
        "extra_groups": [],            # из комментариев
        "extra_groups_raw": [],        # сырой текст из комментариев
    }


def _extract_work_block(text: str) -> str:
    """Вытаскивает блок работы"""
    # Ищем "Работа:" в любом месте строки
    m = re.search(r'[Рр]абота\s*:\s*(.+?)(?=\s*[1-8]️⃣|\s*Заминка|\s*Объём|\Z)',
                  text, re.DOTALL)
    if m:
        work = m.group(1).strip()
        # Убираем markdown и лишние строки
        work = re.sub(r'\*+', '', work).strip()
        # Берём только первые строки до групп
        lines = [l.strip() for l in work.split('\n') if l.strip()]
        # Останавливаемся на первой эмодзи
        result = []
        for line in lines:
            if any(emoji in line for emoji in EMOJI_NUMBERS):
                break
            result.append(line)
        return '\n'.join(result)
    return ""


def _extract_groups_block(text: str) -> str:
    """
    Вытаскивает блок групп — от первой эмодзи до "Заминка:" или "Группа ЗДОРОВЬЯ".
    """
    lines = text.split('\n')
    groups_lines = []
    in_groups = False

    for line in lines:
        line_clean = line.strip()

        # Начало — первая эмодзи-группа
        if any(emoji in line_clean for emoji in EMOJI_NUMBERS):
            in_groups = True

        # Конец — заминка или группа здоровья или ⏺️
        if in_groups and any(kw in line_clean.lower() for kw in
                             ['заминка', 'здоровья', 'объём', 'объем', '⏺']):
            break

        if in_groups and line_clean:
            clean = re.sub(r'\*+', '', line_clean).strip()
            if clean:
                groups_lines.append(clean)

    return '\n'.join(groups_lines) if groups_lines else ""


def parse_extra_group_from_comment(text: str) -> dict | None:
    """
    Парсит дополнительную группу из комментария.
    Форматы:
    - "5️⃣ Группа ..."  (эмодзи-цифра в начале любой из первых строк)
    - "5 группа", "группа 5", "группа 3.5"
    """
    first_lines = text.strip().split('\n')[:3]
    first_lower = '\n'.join(first_lines).lower()

    # Ищем эмодзи-цифру в начале каждой из первых строк
    for line in first_lines:
        line_stripped = line.strip()
        for emoji, num in EMOJI_NUMBERS.items():
            if line_stripped.startswith(emoji):
                return {"number": num, "raw_text": text.strip()}

    # Текстовые паттерны: "группа 3.5", "3.5 группа" и т.д.
    patterns = [
        r'^(\d+(?:[.,]\d+)?)\s*группа',
        r'группа\s*(\d+(?:[.,]\d+)?)',
    ]
    for pattern in patterns:
        m = re.search(pattern, first_lower, re.MULTILINE)
        if m:
            group_num = m.group(1).replace(',', '.')
            return {"number": group_num, "raw_text": text.strip()}

    return None


def _extract_date_and_weekday(text: str) -> tuple:
    first_line = text.strip().split('\n')[0].lower()

    weekday_name = None
    for day in WEEKDAYS_RU:
        if day in first_line:
            weekday_name = day
            break

    m = re.search(r'(\d{1,2})\.(\d{2})\b', text[:50])
    if m:
        day_n, month_n = int(m.group(1)), int(m.group(2))
        year = datetime.now().year
        try:
            dt = datetime(year, month_n, day_n)
            return dt.strftime("%Y-%m-%d"), weekday_name
        except ValueError:
            pass

    return None, weekday_name


def _get_workout_type(weekday_name: str | None) -> str:
    if not weekday_name:
        return 'unknown'
    weekday_num = WEEKDAYS_RU.get(weekday_name, -1)
    if weekday_num in [1, 4]:
        return 'interval'
    if weekday_num == 2:
        return 'hills'
    if weekday_num == 6:
        return 'long'
    return 'unknown'


def _extract_location(text: str) -> str | None:
    m = re.search(r'`([^`]+)`', text)
    if m:
        return m.group(1).strip()
    m = re.search(r'\[([^\]]+)\]\(https?://yandex\.ru/maps[^\)]+\)', text)
    if m:
        return m.group(1).strip()
    return None


def _extract_schedule(text: str) -> str:
    lines = text.split('\n')
    result = []
    for line in lines:
        line = line.strip()
        clean = re.sub(r'\*+', '', line)  # убираем markdown **
        if re.search(r'\d+:\d{2}\s*[–-]', clean) and any(
            kw in clean.lower() for kw in ['сбор', 'старт', 'разминк']
        ):
            result.append(clean)
    return '\n'.join(result)


def _extract_volume(text: str) -> str | None:
    m = re.search(r'[Оо]бъ[её]м[^=\n]*=\s*([\d.,]+)\s*км', text)
    if m:
        return f"{m.group(1)} км"
    return None


_LONG_RUN_LABELS = {
    'красивые': 'КРАСИВЫЕ',
    'здоровья': 'ЗДОРОВЬЯ',
    'здоровье': 'ЗДОРОВЬЯ',
    'беговой релакс': 'Беговой Релакс',
    'релакс': 'Беговой Релакс',
}


def _parse_long_run_groups(text: str) -> list[dict]:
    groups = []
    lines = text.split('\n')
    in_groups = False

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        clean = re.sub(r'\*+', '', line_stripped)
        line_lower = line_stripped.lower()

        has_emoji = any(emoji in line_stripped for emoji in EMOJI_NUMBERS)
        if has_emoji:
            in_groups = True
        elif not in_groups and any(k in line_lower for k in _LONG_RUN_LABELS):
            in_groups = True

        if not in_groups:
            continue

        # Ссылки на маршрут — пропускаем, не останавливаемся
        if 'маршрут' in line_lower or line_stripped.startswith('🚩'):
            continue

        # Стоп-слова — конец блока групп
        if any(kw in line_lower for kw in ['объём', 'объем', 'заминка', '⏺']):
            break

        # Именованная группа
        named_label = None
        for key, label in _LONG_RUN_LABELS.items():
            if key in line_lower:
                named_label = label
                break

        if named_label:
            grp_num = None
            for emoji, num in EMOJI_NUMBERS.items():
                if emoji in line_stripped:
                    grp_num = num
                    break
            paces = re.findall(r'\d+:\d{2}', clean)
            progression = bool(re.search(r'прогресс', line_stripped, re.IGNORECASE))
            pace_start = paces[0] if paces else None
            pace_end = paces[-1] if len(paces) >= 2 else None
            if not pace_end and progression and pace_start:
                parts_p = pace_start.split(':')
                total_p = int(parts_p[0]) * 60 + int(parts_p[1]) - 30
                pace_end = f"{total_p // 60}:{total_p % 60:02d}"
            groups.append({
                "number": grp_num,
                "label": named_label,
                "pace_start": pace_start,
                "pace_end": pace_end,
                "progression": progression,
            })
            continue

        group_num = None
        for emoji, num in EMOJI_NUMBERS.items():
            if emoji in line_stripped:
                group_num = num
                break

        if not group_num:
            continue

        progression = bool(re.search(r'прогресс', line_stripped, re.IGNORECASE))
        paces = re.findall(r'\d+:\d{2}', clean)
        if not paces:
            continue

        pace_start = paces[0]
        pace_end = paces[-1] if len(paces) >= 2 else None

        if not pace_end and progression:
            parts = pace_start.split(':')
            total = int(parts[0]) * 60 + int(parts[1]) - 30
            pace_end = f"{total // 60}:{total % 60:02d}"

        groups.append({
            "number": group_num,
            "label": None,
            "pace_start": pace_start,
            "pace_end": pace_end,
            "progression": progression,
        })

    return groups


def _parse_long_run_post(text: str, post_date: datetime) -> dict | None:
    workout_date, weekday_name = _extract_date_and_weekday(text)
    if not workout_date:
        workout_date = post_date.strftime("%Y-%m-%d")

    location = _extract_location(text)
    schedule = _extract_schedule(text)
    total_volume = _extract_volume(text)
    groups = _parse_long_run_groups(text)
    text_lower = text.lower()
    even_pace_available = bool(re.search(r'без прогресс|ровным темпом|ровный темп', text_lower))

    return {
        "post_date": post_date.strftime("%Y-%m-%d %H:%M"),
        "workout_date": workout_date,
        "weekday": weekday_name or "воскресенье",
        "workout_type": "long",
        "location": location,
        "schedule": schedule,
        "total_volume_km": total_volume or "~18-22 км",
        "groups_raw": text,
        "groups": groups,
        "even_pace_available": even_pace_available,
    }


async def find_next_long_run() -> dict | None:
    """Ищет ближайший воскресный Long Run (100 минут) в канале."""
    today = date.today()
    posts = await get_recent_posts(limit=50)

    future_workouts = []
    past_workouts = []

    for post in posts:
        text_lower = post["text"].lower()
        if '100 минут' not in text_lower and 'ddlong' not in text_lower:
            continue
        if not any(d in text_lower for d in ('воскресенье', 'воскресен')):
            continue

        parsed = _parse_long_run_post(post["text"], post["date"])
        if not parsed:
            continue

        try:
            workout_date = datetime.strptime(parsed["workout_date"], "%Y-%m-%d").date()
        except ValueError:
            continue

        if workout_date >= today:
            parsed["is_past"] = False
            future_workouts.append(parsed)
        else:
            parsed["is_past"] = True
            past_workouts.append(parsed)

    if future_workouts:
        future_workouts.sort(key=lambda x: x["workout_date"])
        return future_workouts[0]
    if past_workouts:
        past_workouts.sort(key=lambda x: x["workout_date"], reverse=True)
        return past_workouts[0]
    return None


async def get_latest_workout_post_id() -> int | None:
    """Возвращает id последнего поста с тренировкой вт/пт (не воскресный Long Run)."""
    posts = await get_recent_posts(limit=30)
    for post in posts:
        text_lower = post["text"].lower()
        matches = sum(1 for kw in WORKOUT_KEYWORDS if kw in text_lower)
        if matches < 3:
            continue
        parsed = parse_workout(post["text"], post["date"])
        if not parsed:
            continue
        try:
            wd = datetime.strptime(parsed["workout_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if wd.weekday() in [1, 4]:
            return post["id"]
    return None


async def get_latest_long_run_post_id() -> int | None:
    """Возвращает id последнего поста с воскресным Long Run."""
    posts = await get_recent_posts(limit=30)
    for post in posts:
        text_lower = post["text"].lower()
        if '100 минут' not in text_lower and 'ddlong' not in text_lower:
            continue
        if not any(d in text_lower for d in ('воскресенье', 'воскресен')):
            continue
        parsed = _parse_long_run_post(post["text"], post["date"])
        if parsed:
            return post["id"]
    return None


async def get_extra_groups_for_post(post_id: int) -> list:
    """Возвращает список доп. групп из комментариев к посту."""
    comments = await get_post_comments(post_id)
    extra_groups = []
    for comment in comments:
        group = parse_extra_group_from_comment(comment["text"])
        if group:
            extra_groups.append(group)
    return extra_groups


def format_workout_message(workout: dict, admin: bool = False) -> str:
    """Форматирует тренировку для пользователя"""

    if workout.get("is_past"):
        lines = [
            "⏳ Новый анонс ещё не вышел\n",
            f"Последняя тренировка — {workout.get('weekday', '').capitalize()} {workout.get('workout_date', '')}:",
            f"📍 {workout.get('location', '')}",
        ]
        if workout.get("work_text"):
            lines.append(f"\n💪 {workout['work_text']}")
        if workout.get("total_volume_km"):
            lines.append(f"📏 Объём: {workout['total_volume_km']}")
        lines.append("\nАнонс следующей тренировки выходит накануне утром.")
        return '\n'.join(lines)

    type_label = {
        'interval': '⚡ Интервальная тренировка',
        'long': '🕐 Длительная (100 мин)',
        'hills': '⛰️ Забегания в гору',
        'unknown': '🏃 Тренировка'
    }.get(workout['workout_type'], '🏃 Тренировка')

    lines = [f"{type_label}\n"]
    if workout.get("workout_date"):
        day = workout.get("weekday", "").capitalize()
        lines.append(f"📅 {day} {workout['workout_date']}")
    if workout.get("location"):
        lines.append(f"📍 {workout['location']}")
    if workout.get("schedule"):
        lines.append(f"⏰ {workout['schedule']}")
    if workout.get("work_text"):
        lines.append(f"\n💪 Работа: {workout['work_text']}")
    if workout.get("total_volume_km"):
        lines.append(f"📏 Объём: {workout['total_volume_km']}")
    if workout.get("groups_raw"):
        lines.append(f"\nГруппы:\n{workout['groups_raw']}")
    if workout.get("extra_groups"):
        nums = [g["number"] for g in workout["extra_groups"]]
        lines.append(f"\nДополнительные группы из комментариев: {', '.join(nums)}")

    # Для админа — полный дебаг
    if admin:
        lines.append("\n\n─── DEBUG ───")
        lines.append(f"Тип: {workout['workout_type']}")
        lines.append(f"Дата поста: {workout['post_date']}")
        if workout.get("extra_groups_raw"):
            lines.append("\nКомментарии с группами:")
            for raw in workout["extra_groups_raw"]:
                lines.append(f"---\n{raw[:200]}")

    return '\n'.join(lines)


async def test_reader():
    print(f"Читаем канал {CHANNEL}...")
    workout = await find_next_workout(only_interval=True)

    if workout:
        print(f"✅ Тренировка найдена!\n")
        print(f"День: {workout['weekday']} / Дата: {workout['workout_date']}")
        print(f"Тип: {workout['workout_type']}")
        print(f"Локация: {workout['location']}")
        print(f"\nРабота:\n{workout['work_text']}")
        print(f"\nГруппы (сырой текст):\n{workout['groups_raw']}")
        print(f"\nДополнительные группы: {[g['number'] for g in workout.get('extra_groups', [])]}")
        if workout.get("extra_groups_raw"):
            print("\nКомментарии:")
            for raw in workout["extra_groups_raw"]:
                print(f"---\n{raw[:300]}")
    else:
        print("❌ Тренировка не найдена")


def _test_parse_extra_group():
    cases = [
        ("5️⃣ группа 4:30 мин/км\n40 человек", "5"),
        ("2️⃣\nТемп 5:00", "2"),
        ("Участвую!\n3️⃣ группа", "3"),
        ("группа 3.5 — добавляем слот", "3.5"),
        ("группа 3,5 дополнительная", "3.5"),
        ("4 группа набор", "4"),
        ("Группа 2 открыта", "2"),
        ("просто текст без группы", None),
    ]
    print("=== Тест parse_extra_group_from_comment ===")
    all_ok = True
    for text, expected in cases:
        result = parse_extra_group_from_comment(text)
        got = result["number"] if result else None
        status = "OK" if got == expected else "FAIL"
        if got != expected:
            all_ok = False
        print(f"[{status}] {repr(text[:40])} => {got!r} (expected {expected!r})")
    print("=== All OK ===\n" if all_ok else "=== FAILURES ===\n")


if __name__ == "__main__":
    import asyncio
    _test_parse_extra_group()
    asyncio.run(test_reader())