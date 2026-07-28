"""Шаг 3 синхронизации: чистка challenger-промта рекомендации (champion не трогает).

Что делает (только в теле build_evening_prompt_challenger):
  - два блока «ВАЖНО про темп» и «ВАЖНО про прогрессию» → один компактный
    «СЕМАНТИКА ТЕМПА» с примером из РЕАЛЬНОГО lt_pace атлета (не захардкоженный 4:17);
  - из «АНАЛИЗ ВОССТАНОВИТЕЛЬНЫХ ОТРЕЗКОВ» убирает второй пример (500м – 1:52-1:42);
  - убирает дублирующую строку «ЗАДАЧА: дай рекомендацию…» (повторяет Правила 2-3).
Идемпотентен, с предохранителем по счётчикам. Запуск: python scripts/patch_challenger_2.py
"""
P = r"D:\running-bot\src\claude_advisor.py"

with open(P, encoding="utf-8") as f:
    lines = f.readlines()

src = "".join(lines)
if "СЕМАНТИКА ТЕМПА" in src:
    print("Патч уже применён — выходим.")
    raise SystemExit(0)

ch_start = next(i for i, l in enumerate(lines)
                if l.startswith("def build_evening_prompt_challenger("))

DROP_PREFIXES = (
    '        "ВАЖНО про темп:',
    '        f"Темп 3:45 мин/км БЫСТРЕЕ',
    '        "Если темп группы МЕНЬШЕ порогового',
    '        "Если темп группы БОЛЬШЕ порогового',
    '        "Используй это при оценке подходимости групп и расч',
    '        "ВАЖНО про прогрессию темпа:',
    '        "Темп 4:05 → 3:40',
    '        "Темп уменьшается в минутах',
    "        \"НЕ писать 'темп снижается'",
    '        "Правильно:',
    '        "Неправильно:',
    "        \"Формат '500м",
    '        "медленный: 112',
    '        "быстрый: 102',
    '        "ЗАДАЧА: дай рекомендацию по группе',
)
NEW_BLOCK = [
    '        "СЕМАНТИКА ТЕМПА: темп в мин:сек на км, меньшее число = БЫСТРЕЕ.",\n',
    '        (f"Пример: {_add_sec_to_pace(lt_pace, -30)} мин/км БЫСТРЕЕ порогового {lt_pace} мин/км."\n',
    '         if lt_pace else "Пример: 3:45 мин/км быстрее 4:17 мин/км."),\n',
    '        "Темп группы МЕНЬШЕ порогового → работа ВЫШЕ ПАНО; БОЛЬШЕ порогового → НИЖЕ ПАНО.",\n',
    '        "Используй это при оценке подходимости групп (suitability_percentages).",\n',
    '        "Прогрессия 4:05 → 3:40 — это УСКОРЕНИЕ: темп растёт; писать «ускорение», не «темп снижается».",\n',
]

out = lines[:ch_start]
inserted = dropped = 0
for l in lines[ch_start:]:
    if any(l.startswith(p) for p in DROP_PREFIXES):
        dropped += 1
        if l.startswith('        "ВАЖНО про темп:'):
            out.extend(NEW_BLOCK)
            inserted += 1
        continue
    out.append(l)

if inserted != 1 or dropped != 15:
    print(f"ОШИБКА: inserted={inserted}, dropped={dropped} (ожидалось 1 и 15) — файл не тронут")
    raise SystemExit(1)

with open(P, "w", encoding="utf-8", newline="") as f:
    f.writelines(out)
print(f"OK: семантика темпа слита, {dropped} строк убрано (только challenger)")
