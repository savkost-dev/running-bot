#!/usr/bin/env python3
"""
Патч: добавляем второе сообщение варианта B — свободный текст от ИИ
1. claude_advisor.py: новая функция generate_ai_b_extra()
2. bot.py: _send_ai_variant_b отправляет второе сообщение
"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))
CA_PATH = os.path.join(BASE, "src", "claude_advisor.py")
BOT_PATH = os.path.join(BASE, "src", "bot.py")

# ============================================================
# 1. claude_advisor.py — добавляем generate_ai_b_extra после format_ai_b_message
# ============================================================
with open(CA_PATH, "r", encoding="utf-8") as f:
    ca = f.read()

changes = 0

new_function = '''

def generate_ai_b_extra(analysis: dict, advice: dict, mode: str = "smart") -> str:
    """Второе сообщение варианта B — свободный текст от ИИ.
    Получает уже заполненный advice_dict и пишет живым языком
    то, что не влезло в шаблон: нюансы, физиология, наблюдения.
    Возвращает текст (HTML-совместимый) или пустую строку при ошибке.
    """
    import re as _re

    group = advice.get("recommended_group", "?")
    pace = advice.get("recommended_pace", "?")
    reason = advice.get("reason", "")
    overall = analysis.get("overall_purpose", "")
    summary = analysis.get("summary", "")
    block_contrast = analysis.get("block_contrast", "")
    target_athlete = analysis.get("target_athlete", "")
    what_to_watch = analysis.get("what_to_watch", "")
    intensity = analysis.get("intensity_level", "")

    prompt = (
        "Ты — беговой тренер клуба Dusty Dumbbells.\\n"
        "Только что ты выдал бегуну структурированную рекомендацию. "
        "Теперь напиши короткое дополнение — живым тренерским языком, без шаблонов.\\n\\n"
        "ЧТО УЖЕ СКАЗАНО В ШАБЛОНЕ:\\n"
        f"Рекомендована группа {group}, темп {pace}.\\n"
        f"Обоснование: {reason}\\n\\n"
        "КОНТЕКСТ ТРЕНИРОВКИ:\\n"
        f"Суть: {summary}\\n"
        f"Цель: {overall}\\n"
        f"Контраст блоков: {block_contrast}\\n"
        f"На кого рассчитана: {target_athlete}\\n"
        f"Интенсивность: {intensity}\\n"
        f"На что смотреть: {what_to_watch}\\n\\n"
        "ЗАДАЧА: напиши 3–5 предложений — то, что не влезает в шаблон:\\n"
        "- физиология этой тренировки (что происходит в теле)\\n"
        "- почему именно такой подбор с учётом состояния бегуна сегодня\\n"
        "- конкретный совет на первые 2–3 отрезка\\n\\n"
        "Правила:\\n"
        "- Только теги <b>жирный</b> и <i>курсив</i>, никакого markdown\\n"
        "- Без вводных фраз типа 'Дополнение:', 'Итак:' — сразу по делу\\n"
        "- Живо, конкретно, по-тренерски"
    )

    if mode == "deep":
        model, max_tok, timeout = MODEL_DEEP, 1000, 120
    else:
        model, max_tok, timeout = MODEL_SMART, 800, 60

    try:
        resp = _get_client().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tok,
            temperature=0.5,
            timeout=timeout,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
        return raw
    except Exception as e:
        logger.warning(f"generate_ai_b_extra error: {e}")
        return ""
'''

# Вставляем после format_ai_b_message
anchor = "\ndef _pace_sec(pace: str) -> float | None:"
if anchor in ca:
    ca = ca.replace(anchor, new_function + anchor, 1)
    print("✅ generate_ai_b_extra added to claude_advisor.py")
    changes += 1
else:
    print("❌ anchor '_pace_sec' not found")

with open(CA_PATH, "w", encoding="utf-8") as f:
    f.write(ca)
print(f"claude_advisor.py saved ({changes} changes)")

# ============================================================
# 2. bot.py — _send_ai_variant_b: добавляем второе сообщение
# ============================================================
with open(BOT_PATH, "r", encoding="utf-8") as f:
    bt = f.read()

changes_bot = 0

old_end = (
    '        msg_text = claude_advisor.format_evening_message(\n'
    '            advice, workout_for_render, stats\n'
    '        )\n'
    '        await context.bot.send_message(telegram_id, msg_text, parse_mode="HTML")'
)
new_end = (
    '        msg_text = claude_advisor.format_evening_message(\n'
    '            advice, workout_for_render, stats\n'
    '        )\n'
    '        await context.bot.send_message(telegram_id, msg_text, parse_mode="HTML")\n'
    '        # Второе сообщение — свободный текст от ИИ (нюансы, физиология)\n'
    '        extra = await asyncio.get_event_loop().run_in_executor(\n'
    '            None,\n'
    '            functools.partial(\n'
    '                claude_advisor.generate_ai_b_extra, analysis, advice, rec_mode\n'
    '            )\n'
    '        )\n'
    '        if extra:\n'
    '            await context.bot.send_message(\n'
    '                telegram_id,\n'
    '                f"🧪 <b>Дополнение B</b>\\n\\n{extra}",\n'
    '                parse_mode="HTML"\n'
    '            )'
)

if old_end in bt:
    bt = bt.replace(old_end, new_end, 1)
    print("✅ _send_ai_variant_b patched — second message added")
    changes_bot += 1
else:
    print("❌ _send_ai_variant_b end — not found")
    idx = bt.find("format_evening_message")
    print(f"   format_evening_message occurrences: {bt.count('format_evening_message')}")

with open(BOT_PATH, "w", encoding="utf-8") as f:
    f.write(bt)
print(f"bot.py saved ({changes_bot} changes)")

total = changes + changes_bot
print(f"\n{'='*40}")
print(f"ИТОГО: {total}/2 изменений применено")
if total == 2:
    print("✅ Готово к деплою!")
else:
    print("⚠️  Проверь вывод выше")
