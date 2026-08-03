"""Температура для брифа: ask_text получает параметр temperature (дефолт 0.4 —
все текущие вызовы не меняются), бриф зовёт с 0.2 для стабильности формулировок.
Идемпотентен, с предохранителями. Запуск: python scripts/patch_temp.py"""
import re

CA = r"D:\running-bot\src\claude_advisor.py"
AB = r"D:\running-bot\src\announce_brief.py"

with open(CA, encoding="utf-8") as f:
    ca = f.read()
if "temperature: float = 0.4" in ca:
    print("claude_advisor уже пропатчен")
else:
    m = re.search(r"def ask_text\(([^)]*)\)( -> [^:]+)?:", ca)
    if not m:
        print("ОШИБКА: def ask_text не найден — ничего не меняю")
        raise SystemExit(1)
    new_sig = f"def ask_text({m.group(1)}, temperature: float = 0.4){m.group(2) or ''}:"
    start = m.start()
    end = ca.find("\ndef ", m.end())
    body = ca[m.end():end]
    if body.count("temperature=0.4,") != 1:
        print(f"ОШИБКА: в теле ask_text temperature=0.4 встретился "
              f"{body.count('temperature=0.4,')} раз (ожидался 1) — ничего не меняю")
        raise SystemExit(1)
    ca = (ca[:start] + new_sig
          + body.replace("temperature=0.4,", "temperature=temperature,")
          + ca[end:])
    with open(CA, "w", encoding="utf-8", newline="") as f:
        f.write(ca)
    print("OK: ask_text принимает temperature (дефолт 0.4 — поведение не изменилось)")

with open(AB, encoding="utf-8") as f:
    ab = f.read()
if "temperature=0.2" in ab:
    print("announce_brief уже пропатчен")
else:
    old = "raw = claude_advisor.ask_text(\n        _MODES_PROMPT + json.dumps(payload, ensure_ascii=False), mode)"
    if old not in ab:
        print("ОШИБКА: вызов ask_text в announce_brief не найден — ничего не меняю")
        raise SystemExit(1)
    ab = ab.replace(old, "raw = claude_advisor.ask_text(\n        _MODES_PROMPT + json.dumps(payload, ensure_ascii=False), mode, temperature=0.2)")
    with open(AB, "w", encoding="utf-8", newline="") as f:
        f.write(ab)
    print("OK: бриф вызывает ask_text с temperature=0.2")
