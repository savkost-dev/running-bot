#!/usr/bin/env python3
"""
Патч Задача 2 v2: правильная архитектура варианта B
- build_ai_b_prompt → ask_groq → advice_dict → format_evening_message
- generate_ai_b_recommendation не используется
- Фикс f-string с фигурными скобками в промпте
"""
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
CA_PATH = os.path.join(BASE, "src", "claude_advisor.py")
BOT_PATH = os.path.join(BASE, "src", "bot.py")

# ============================================================
# claude_advisor.py
# ============================================================
with open(CA_PATH, "r", encoding="utf-8") as f:
    ca = f.read()

changes = 0

# --- 1. Фикс build_ai_b_prompt: заменить f-string на обычную строку ---
# Находим функцию build_ai_b_prompt и меняем её концовку
# Проблема: в f-string фигурные скобки JSON интерпретируются как плейсхолдеры

old_return = '''    return f"""Ты — беговой тренер клуба Dusty Dumbbells.
Подбери группу для бегуна. Отвечай тренерски — без шаблонов, живым языком.

ТРЕНИРОВКА: {analysis.get('workout_date', '—')}
Суть: {analysis.get('summary', '—')}
Цель: {analysis.get('overall_purpose', '—')}
На что обратить внимание: {analysis.get('what_to_watch', '—')}

СТРУКТУРА (одинаково для всех):
{struct_text}

ГРУППЫ (отличаются только темпами):
{groups_text}

ДАННЫЕ БЕГУНА:
Зоны темпа:
{zones_text}
Специализация: {spec_label}
Восстановление: {rec_text}

Дай рекомендацию строго в формате JSON:

{
  "recommended_group": "номер группы (например: 3)",
  "recommended_pace": "темп основной группы (например: 4:00–4:25 мин/км)",
  "reason": "1-2 предложения: почему эта группа — укажи зоны и восстановление",
  "if_feeling_good": "что делать если ноги бегут легко на разминке — группа выше с темпом",
  "if_tired": "что делать если тяжело — группа ниже или промежуточный темп X.5 с цифрами",
  "gap_note": "вывод по разрывам: небольшой (можно переходить) или большой (нужна промежуточная)",
  "suitability_percentages": [
    {"group": "номер группы", "percentage": число_от_0_до_100, "comment": "СТРОГО 1-2 слова"}
  ],
  "preparation_tips": ["совет 1", "совет 2"],
  "warning": "предупреждение или null"
}

Рассмотри альтернативы: какая группа для скорости, какая для восстановления.
Группа с максимальным percentage ДОЛЖНА совпадать с recommended_group.
Поле 'group' в suitability_percentages — ТОЛЬКО номер (допустимо: '1','2','3','3.5','4','5').
Отвечай только JSON, без лишнего текста."""'''

new_return = '''    json_schema = (
        "{"
        + '"recommended_group": "номер группы (например: 3)", '
        + '"recommended_pace": "темп основной группы (например: 4:00–4:25 мин/км)", '
        + '"reason": "1-2 предложения: почему эта группа — укажи зоны и восстановление", '
        + '"if_feeling_good": "что делать если ноги бегут легко на разминке — группа выше с темпом", '
        + '"if_tired": "что делать если тяжело — группа ниже или промежуточный темп X.5 с цифрами", '
        + '"gap_note": "вывод по разрывам: небольшой (можно переходить) или большой (нужна промежуточная)", '
        + '"suitability_percentages": [{"group": "номер", "percentage": 0..100, "comment": "1-2 слова"}], '
        + '"preparation_tips": ["совет 1"], '
        + '"warning": "предупреждение или null"'
        + "}"
    )
    return (
        "Ты — беговой тренер клуба Dusty Dumbbells.\\n"
        "Подбери группу для бегуна. Отвечай тренерски — без шаблонов, живым языком.\\n\\n"
        f"ТРЕНИРОВКА: {analysis.get('workout_date', '—')}\\n"
        f"Суть: {analysis.get('summary', '—')}\\n"
        f"Цель: {analysis.get('overall_purpose', '—')}\\n"
        f"На что обратить внимание: {analysis.get('what_to_watch', '—')}\\n\\n"
        "СТРУКТУРА (одинаково для всех):\\n"
        f"{struct_text}\\n\\n"
        "ГРУППЫ (отличаются только темпами):\\n"
        f"{groups_text}\\n\\n"
        "ДАННЫЕ БЕГУНА:\\n"
        "Зоны темпа:\\n"
        f"{zones_text}\\n"
        f"Специализация: {spec_label}\\n"
        f"Восстановление: {rec_text}\\n\\n"
        "Дай рекомендацию строго в формате JSON:\\n\\n"
        f"{json_schema}\\n\\n"
        "Рассмотри альтернативы: какая группа для скорости, какая для восстановления.\\n"
        "Группа с максимальным percentage ДОЛЖНА совпадать с recommended_group.\\n"
        "Поле 'group' в suitability_percentages — ТОЛЬКО номер (допустимо: '1','2','3','3.5','4','5').\\n"
        "ЗАПРЕЩЕНО: 'Группа 3', '3 быстрая'. Только цифра или цифра с точкой.\\n"
        "Отвечай только JSON, без лишнего текста."
    )'''

if old_return in ca:
    ca = ca.replace(old_return, new_return, 1)
    print("✅ build_ai_b_prompt return patched (f-string → concat, no brace issues)")
    changes += 1
else:
    print("❌ build_ai_b_prompt return — not found")
    # Try partial
    idx = ca.find('    return f"""Ты — беговой тренер клуба Dusty Dumbbells.')
    if idx >= 0:
        print(f"   Found return at {idx}, printing end:")
        # Find the end of this return statement
        end = ca.find('\n\ndef ', idx)
        print(repr(ca[idx:idx+300]))
        print("   ...")
        print(repr(ca[end-200:end+10] if end > 0 else ca[idx+1800:idx+2100]))

with open(CA_PATH, "w", encoding="utf-8") as f:
    f.write(ca)
print(f"claude_advisor.py saved ({changes} changes)")

# ============================================================
# bot.py — _send_ai_variant_b: используем ask_groq вместо generate_ai_b_recommendation
# ============================================================
with open(BOT_PATH, "r", encoding="utf-8") as f:
    bt = f.read()

changes_bot = 0

old_send = (
    '        advice, stats = await asyncio.get_event_loop().run_in_executor(\n'
    '            None,\n'
    '            functools.partial(\n'
    '                claude_advisor.generate_ai_b_recommendation,\n'
    '                analysis, user_data, zones_map, recovery, rec_mode\n'
    '            )\n'
    '        )\n'
    '        stats["mode"] = "b_ai"\n'
    '        # Формируем workout dict из analysis для единого рендерера\n'
    '        workout_for_render = {\n'
    '            "workout_type": analysis.get("workout_type", "interval"),\n'
    '            "workout_date": analysis.get("workout_date", ""),\n'
    '            "location": analysis.get("location", ""),\n'
    '            "schedule": "",\n'
    '            "work_text": "",\n'
    '        }\n'
    '        msg_text = claude_advisor.format_evening_message(\n'
    '            advice, workout_for_render, stats\n'
    '        )\n'
    '        await context.bot.send_message(telegram_id, msg_text, parse_mode="HTML")'
)
new_send = (
    '        prompt = claude_advisor.build_ai_b_prompt(analysis, user_data, zones_map, recovery)\n'
    '        result = await asyncio.get_event_loop().run_in_executor(\n'
    '            None,\n'
    '            functools.partial(claude_advisor.ask_groq, prompt, rec_mode)\n'
    '        )\n'
    '        if not result or not result.get("advice"):\n'
    '            logger.warning("_send_ai_variant_b: ask_groq returned no advice")\n'
    '            return\n'
    '        advice = result["advice"]\n'
    '        stats = result.get("stats", {})\n'
    '        stats["mode"] = "b_ai"\n'
    '        # Поля из анализа (Шаг 1) — подставляем в коде, ИИ не дублирует\n'
    '        spec = (get_preferences(db_user_id) or {}).get("specialization") or "half_marathon"\n'
    '        advice["overall_purpose"] = analysis.get("overall_purpose", "")\n'
    '        advice["workout_summary"] = analysis.get("summary", "")\n'
    '        advice["spec_label"] = claude_advisor._SPEC_LABELS.get(spec, spec)\n'
    '        # Санитайз номеров групп\n'
    '        for item in (advice.get("suitability_percentages") or []):\n'
    '            if "group" in item:\n'
    '                item["group"] = claude_advisor._sanitize_group_name(str(item["group"]))\n'
    '        workout_for_render = {\n'
    '            "workout_type": analysis.get("workout_type", "interval"),\n'
    '            "workout_date": analysis.get("workout_date", ""),\n'
    '            "location": analysis.get("location", ""),\n'
    '            "schedule": "",\n'
    '            "work_text": "",\n'
    '        }\n'
    '        msg_text = claude_advisor.format_evening_message(\n'
    '            advice, workout_for_render, stats\n'
    '        )\n'
    '        await context.bot.send_message(telegram_id, msg_text, parse_mode="HTML")'
)

if old_send in bt:
    bt = bt.replace(old_send, new_send, 1)
    print("✅ _send_ai_variant_b patched → uses ask_groq (single parser)")
    changes_bot += 1
else:
    print("❌ _send_ai_variant_b — not found")
    idx = bt.find("generate_ai_b_recommendation")
    if idx >= 0:
        print(f"   generate_ai_b_recommendation found at {idx}")
        print(repr(bt[max(0, idx-100):idx+300]))

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
