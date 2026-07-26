"""Бриф анонса для админа (Работа+Цель+Суть + режимы блоками).

Изолированный модуль: ничего из боевого не импортирует кроме claude_advisor.ask_text
и database.get_connection. Вызывается из _autoanalyze_post ПОСЛЕ штатного
уведомления админа; любая ошибка внутри не роняет автоанализ (вызов обёрнут).

Шаг 1.5: по work_text/структуре/группам ИИ строит список элементов работы с
режимами (интенсивность относительно ПАНО/МПК + расшифровка). Результат
кэшируется в analyzed_json["modes"] по post_id — для будущих комментариев
к анонсу пересчитывать не придётся.

Главная точка: build_admin_brief(result, post_id, mode) -> str | None.
"""
import json
import logging

logger = logging.getLogger(__name__)

_MODES_PROMPT = (
    "Ты тренер бегового клуба. По данным тренировки составь список элементов работы "
    "с рекомендуемыми режимами.\n"
    "Ответ — СТРОГО JSON-массив, без пояснений и Markdown:\n"
    '[{"element": "название (Фоновый бег / Вставка 400 м / Отдых ...)", '
    '"dist": "дистанция или объём", '
    '"intensity": "скоростной режим в терминах ПАНО/МПК (например: чуть выше ПАНО; 90-95% от ПАНО; МПК + 5 сек/км)", '
    '"note": "1-2 предложения расшифровки: ощущения, дыхание, зачем этот элемент"}]\n'
    "Правил: элементов столько, сколько реально разных режимов в работе; "
    "не выдумывай отдых, если его нет в структуре; пиши по-русски. "
    "Это ОБЩАЯ рекомендация для атлета-любителя средней тренированности, который "
    "целенаправленно тренируется ради роста результатов (не оздоровительный бег «лишь бы "
    "добежать»), но не персональная: "
    "НЕ указывай абсолютные значения пульса в уд/мин и абсолютные темпы — только "
    "относительно ПАНО/МПК (в том числе сдвиги вида «МПК + 5 сек/км»). Сдвиги формулируй "
    "словами «быстрее»/«медленнее», а не «плюс»/«минус» — с темпом в мин/км это двусмысленно "
    "(например: «на 3-5 сек/км быстрее ПАНО», «на 10-15 сек/км медленнее ПАНО»), "
    "и через ощущения/дыхание. "
    "Режим выбирай САМ по совокупности факторов — ниже только то, на что обратить внимание: "
    "длина отрезков, число повторов, суммарный объём работы, наличие отдыха и его регламент "
    "(если темп отдыха задан и быстрый, быстрее ~5:30/км — восстановление неполное; "
    "если не регламентирован — почти полное). "
    "Режим должен быть удержим на ВСЁМ объёме задания, а не на одном отрезке. "
    "Если отрезки идут НЕПРЕРЫВНО друг за другом (без отдыха), их режимы связаны: "
    "перепад между соседними элементами делай умеренным — базовая часть в непрерывной "
    "связке это не отдых и не лёгкая трусца, она бежится ближе к рабочим режимам, "
    "ведь восстановления между ними нет. "
    "Шкала непрерывная — от заметно ниже ПАНО до выше МПК; промежуточные варианты "
    "(например «посередине между ПАНО и МПК») допустимы и часто точнее крайних. "
    "Ориентир для калибровки (не правило): классические интервалы 400-1000 м с трусцовым "
    "восстановлением обычно бегут ближе к МПК, чем к ПАНО — на ПАНО такая работа "
    "теряет тренирующий эффект. "
    "Не перестраховывайся: задача — развивающая, а не щадящая рекомендация; "
    "недогруз — такая же ошибка, как перегруз, тренировка не должна терять смысл.\n\n"
    "Данные тренировки:\n"
)


def _modes_from_ai(result: dict, mode: str) -> list | None:
    """Шаг 1.5: список режимов из ИИ. None при сбое (бриф уйдёт без таблицы)."""
    import claude_advisor
    payload = {
        "work_text": result.get("work_text") or "",
        "summary": result.get("summary") or "",
        "structure": result.get("structure") or [],
        "groups": [{"number": g.get("number"), "paces": g.get("paces") or g.get("pace")}
                   for g in (result.get("groups") or [])],
    }
    raw = claude_advisor.ask_text(
        _MODES_PROMPT + json.dumps(payload, ensure_ascii=False), mode)
    if not raw:
        return None
    raw = raw.strip()
    try:
        start, end = raw.index("["), raw.rindex("]") + 1
        modes = json.loads(raw[start:end])
    except Exception as e:
        logger.warning(f"announce_brief: JSON режимов не распарсился: {e}")
        return None
    return modes if isinstance(modes, list) and modes else None


def _cache_modes(post_id: int, modes: list) -> None:
    """Пишет modes внутрь analyzed_json (рядом с summary/overall_purpose)."""
    from database import get_connection
    with get_connection() as conn:
        row = conn.execute(
            "SELECT analyzed_json FROM workout_analysis WHERE post_id = ?",
            (post_id,)).fetchone()
        if not row or not row[0]:
            return
        try:
            d = json.loads(row[0])
        except Exception:
            return
        d["modes"] = modes
        conn.execute(
            "UPDATE workout_analysis SET analyzed_json = ? WHERE post_id = ?",
            (json.dumps(d, ensure_ascii=False), post_id))


def format_brief(result: dict, modes: list | None) -> str:
    """Текст брифа: РЦС + режимы блоками. Без HTML — обычный текст Telegram."""
    lines = []
    wdate = result.get("workout_date") or "—"
    lines.append(f"🧭 Режимы тренировки {wdate}")
    if result.get("work_text"):
        lines.append(f"\n🚪 Работа: {result['work_text']}")
    if result.get("overall_purpose"):
        lines.append(f"🏁 Цель: {result['overall_purpose']}")
    if result.get("summary"):
        lines.append(f"💡 Суть: {result['summary']}")
    lines.append("\n⚠️ Если у тебя есть тренер — его задание главнее: он знает твою форму и цели. "
                 "Для всех остальных ориентиром послужит рекомендация ниже.")
    if modes:
        lines.append("\n📊 Рекомендуемые режимы:")
        for m in modes:
            el = (m.get("element") or "").strip()
            dist = (m.get("dist") or "").strip()
            inten = (m.get("intensity") or "").strip()
            note = (m.get("note") or "").strip()
            head = f"🔹 {el}" + (f" — {dist}" if dist else "")
            lines.append(f"\n{head}")
            if inten:
                lines.append(f"Интенсивность: {inten}")
            if note:
                lines.append(note)
    # Плейсхолдер под пару предложений от Антона — текст задаст позже.
    return "\n".join(lines)


def build_admin_brief(result: dict, post_id: int, mode: str) -> str | None:
    """Собирает бриф. Синхронный (ИИ-вызов внутри) — вызывать через asyncio.to_thread.
    Только интервальные: для лонга бриф не строится."""
    if not result.get("is_valid") or result.get("workout_type") != "interval":
        return None
    modes = _modes_from_ai(result, mode)
    if modes:
        try:
            _cache_modes(post_id, modes)
        except Exception as e:
            logger.warning(f"announce_brief: кэш modes не записался: {e}")
    return format_brief(result, modes)
