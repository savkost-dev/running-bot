#!/usr/bin/env python3
"""
Патч Задача 2: Вариант B → общий рендерер format_evening_message
Запуск: python patch_task2.py
"""
import sys
import os

BASE = os.path.dirname(os.path.abspath(__file__))
CA_PATH = os.path.join(BASE, "src", "claude_advisor.py")
BOT_PATH = os.path.join(BASE, "src", "bot.py")

with open(CA_PATH, "r", encoding="utf-8") as f:
    ca = f.read()

changes = 0

# --- 1a. _MODE_LABELS: добавить "b_ai" ---
old = '_MODE_LABELS = {"deep": "🧠 Глубокий (ИИ)", "smart": "⚡ Быстрый (ИИ)",\n                "fast": "🪶 Лёгкий (ИИ)", "calc": "📊 Расчётный"}'
new = '_MODE_LABELS = {"deep": "🧠 Глубокий (ИИ)", "smart": "⚡ Быстрый (ИИ)",\n                "fast": "🪶 Лёгкий (ИИ)", "calc": "📊 Расчётный",\n                "b_ai": "🧪 B (ИИ выбор)"}'
if old in ca:
    ca = ca.replace(old, new, 1)
    print("✅ _MODE_LABELS patched")
    changes += 1
else:
    print("❌ _MODE_LABELS — not found")

# --- 1b. build_ai_b_prompt: убрать HTML-инструкцию, добавить JSON-схему ---
old_html = (
    "Дай рекомендацию в формате HTML (как в Telegram).\n"
    "Правила форматирования:\n"
    "- Только теги <b>жирный</b> и <i>курсив</i>\n"
    "- Никакого markdown: без **, без *, без - как маркеров списка\n"
    "- Абзацы разделяй пустой строкой\n"
    "\n"
    "Структура ответа:\n"
    "1. 1-2 предложения: суть тренировки + структура (что и сколько)\n"
    "2. Рекомендация: какая группа и почему (с опорой на зоны и восстановление)\n"
    "3. Что ожидать на тренировке (2-3 предложения)\n"
    "4. Манёвр по разминке: если легко — куда сдвинуться, если тяжело — куда отступить\n"
    "\n"
    "Живой тренерский язык, без воды.\"\"\""
)
new_json = (
    "Дай рекомендацию строго в формате JSON:\n"
    "\n"
    "{\n"
    '  "recommended_group": "номер группы (например: 3)",\n'
    '  "recommended_pace": "темп основной группы (например: 4:00–4:25 мин/км)",\n'
    '  "reason": "1-2 предложения: почему эта группа — укажи зоны и восстановление",\n'
    '  "if_feeling_good": "что делать если ноги бегут легко на разминке — группа выше с темпом",\n'
    '  "if_tired": "что делать если тяжело — группа ниже или промежуточный темп X.5 с цифрами",\n'
    '  "gap_note": "вывод по разрывам: небольшой (можно переходить) или большой (нужна промежуточная)",\n'
    '  "suitability_percentages": [\n'
    '    {"group": "номер группы", "percentage": число_от_0_до_100, "comment": "СТРОГО 1-2 слова"}\n'
    "  ],\n"
    '  "preparation_tips": ["совет 1", "совет 2"],\n'
    '  "warning": "предупреждение или null"\n'
    "}\n"
    "\n"
    "Рассмотри альтернативы: какая группа для скорости, какая для восстановления.\n"
    "Группа с максимальным percentage ДОЛЖНА совпадать с recommended_group.\n"
    "Поле 'group' в suitability_percentages — ТОЛЬКО номер (допустимо: '1','2','3','3.5','4','5').\n"
    'Отвечай только JSON, без лишнего текста."""'
)
if old_html in ca:
    ca = ca.replace(old_html, new_json, 1)
    print("✅ build_ai_b_prompt HTML→JSON schema patched")
    changes += 1
else:
    print("❌ HTML instruction in build_ai_b_prompt — not found")
    idx = ca.find("Дай рекомендацию в формате HTML")
    if idx >= 0:
        print(f"   Found partial at {idx}: {repr(ca[idx:idx+200])}")
    else:
        print("   Partial match not found either — check the prompt ending manually")

# --- 1c. generate_ai_b_recommendation: парсить JSON, возвращать advice_dict ---
old_gen = (
    '    try:\n'
    '        resp = _get_client().chat.completions.create(\n'
    '            model=model,\n'
    '            messages=[{"role": "user", "content": prompt}],\n'
    '            max_tokens=max_tok,\n'
    '            temperature=0.4,\n'
    '            timeout=timeout,\n'
    '        )\n'
    '        usage = resp.usage\n'
    '        stats = {\n'
    '            "time_sec": round(_time.time() - t0, 1),\n'
    '            "mode": mode,\n'
    '            "input_tokens": usage.prompt_tokens if usage else None,\n'
    '            "output_tokens": usage.completion_tokens if usage else None,\n'
    '        }\n'
    '        text = (resp.choices[0].message.content or "").strip()\n'
    '        return text, stats\n'
    '    except Exception as e:\n'
    '        logger.warning(f"generate_ai_b_recommendation error: {e}")\n'
    '        stats["time_sec"] = round(_time.time() - t0, 1)\n'
    '        return "", stats'
)
new_gen = (
    '    try:\n'
    '        resp = _get_client().chat.completions.create(\n'
    '            model=model,\n'
    '            messages=[{"role": "user", "content": prompt}],\n'
    '            max_tokens=max_tok,\n'
    '            temperature=0.4,\n'
    '            timeout=timeout,\n'
    '        )\n'
    '        usage = resp.usage\n'
    '        stats = {\n'
    '            "time_sec": round(_time.time() - t0, 1),\n'
    '            "mode": mode,\n'
    '            "input_tokens": usage.prompt_tokens if usage else None,\n'
    '            "output_tokens": usage.completion_tokens if usage else None,\n'
    '        }\n'
    '        raw = (resp.choices[0].message.content or "").strip()\n'
    '        # Парсим JSON ответ\n'
    '        import re as _re2\n'
    '        raw_clean = _re2.sub(r"<think>.*?</think>", "", raw, flags=_re2.DOTALL).strip()\n'
    '        raw_clean = raw_clean.replace("```json", "").replace("```", "").strip()\n'
    '        advice: dict = {}\n'
    '        try:\n'
    '            advice = json.loads(raw_clean)\n'
    '        except Exception:\n'
    '            m2 = _re2.search(r\'\\{[\\s\\S]*\\}\', raw_clean)\n'
    '            if m2:\n'
    '                try:\n'
    '                    advice = json.loads(m2.group(0))\n'
    '                except Exception:\n'
    '                    advice = {}\n'
    '        if not isinstance(advice, dict):\n'
    '            advice = {}\n'
    '        # Поля из анализа (Шаг 1) — ИИ их не дублирует, подставляем в коде\n'
    '        advice["overall_purpose"] = analysis.get("overall_purpose", "")\n'
    '        advice["workout_summary"] = analysis.get("summary", "")\n'
    '        spec = user_data.get("specialization") or "half_marathon"\n'
    '        advice["spec_label"] = _SPEC_LABELS.get(spec, spec)\n'
    '        # Санитайз номеров групп в suitability_percentages\n'
    '        for item in (advice.get("suitability_percentages") or []):\n'
    '            if "group" in item:\n'
    '                item["group"] = _sanitize_group_name(str(item["group"]))\n'
    '        return advice, stats\n'
    '    except Exception as e:\n'
    '        logger.warning(f"generate_ai_b_recommendation error: {e}")\n'
    '        stats["time_sec"] = round(_time.time() - t0, 1)\n'
    '        return {}, stats'
)
if old_gen in ca:
    ca = ca.replace(old_gen, new_gen, 1)
    print("✅ generate_ai_b_recommendation patched → returns (advice_dict, stats)")
    changes += 1
else:
    print("❌ generate_ai_b_recommendation body — not found")
    idx = ca.find("def generate_ai_b_recommendation")
    if idx >= 0:
        print(f"   Function at {idx}, body doesn't match exactly")
        # Show what's there
        fn_body = ca[idx:idx+600]
        print(f"   {repr(fn_body)}")

with open(CA_PATH, "w", encoding="utf-8") as f:
    f.write(ca)
print(f"claude_advisor.py saved ({changes} changes applied)")

# ============================================================
# 2. bot.py
# ============================================================
with open(BOT_PATH, "r", encoding="utf-8") as f:
    bt = f.read()

changes_bot = 0

old_send = (
    '        text, stats = await asyncio.get_event_loop().run_in_executor(\n'
    '            None,\n'
    '            functools.partial(\n'
    '                claude_advisor.generate_ai_b_recommendation,\n'
    '                analysis, user_data, zones_map, recovery, rec_mode\n'
    '            )\n'
    '        )\n'
    '        msg_text = claude_advisor.format_ai_b_message(text, analysis, stats)\n'
    '        await context.bot.send_message(telegram_id, msg_text, parse_mode="HTML")'
)
new_send = (
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
if old_send in bt:
    bt = bt.replace(old_send, new_send, 1)
    print("✅ _send_ai_variant_b patched (format_ai_b_message → format_evening_message)")
    changes_bot += 1
else:
    print("❌ _send_ai_variant_b body — not found")
    idx = bt.find("format_ai_b_message")
    if idx >= 0:
        print(f"   format_ai_b_message at {idx}")
        print(f"   Context: {repr(bt[max(0,idx-150):idx+200])}")
    else:
        print("   format_ai_b_message not found in bot.py at all")

with open(BOT_PATH, "w", encoding="utf-8") as f:
    f.write(bt)
print(f"bot.py saved ({changes_bot} changes applied)")

total = changes + changes_bot
print(f"\n{'='*40}")
print(f"ИТОГО: {total}/4 изменений применено")
if total < 4:
    print("⚠️  Некоторые изменения не нашли совпадений — проверь вывод выше")
else:
    print("✅ Все 4 изменения применены успешно!")
