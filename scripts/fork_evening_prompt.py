"""Развилка промта рекомендации: champion (боевая, заморожена) + challenger (правим).

Делает из build_evening_prompt три сущности:
  - build_evening_prompt_champion — текущее боевое тело (переименование)
  - build_evening_prompt_challenger — точная копия (для правок)
  - build_evening_prompt — обёртка, выбирает по EVENING_PROMPT_VARIANT

Вызовы в bot.py менять не нужно. Идемпотентен: повторный запуск ничего не делает.
Запуск: python scripts/fork_evening_prompt.py
"""
import shutil

P = r"D:\running-bot\src\claude_advisor.py"

with open(P, encoding="utf-8") as f:
    src = f.read()

if "build_evening_prompt_champion" in src:
    print("Развилка уже сделана — выходим.")
    raise SystemExit(0)

sig = 'def build_evening_prompt(workout: dict, fitness: dict, recovery: dict | None = None, weather_prompt: str = "") -> str:'
start = src.index(sig)
# конец функции = следующий def верхнего уровня после начала
end = src.index("\ndef ", start + len(sig))
body = src[start:end]  # включает def-строку

wrapper = (
    'EVENING_PROMPT_VARIANT = "challenger"  # "champion" — мгновенный откат к боевой версии\n\n\n'
    'def build_evening_prompt(workout: dict, fitness: dict, recovery: dict | None = None, '
    'weather_prompt: str = "") -> str:\n'
    '    """Развилка промта рекомендации: challenger правится, champion заморожен.\n'
    '    Переключение — константой EVENING_PROMPT_VARIANT выше."""\n'
    '    fn = (build_evening_prompt_challenger if EVENING_PROMPT_VARIANT == "challenger"\n'
    '          else build_evening_prompt_champion)\n'
    '    return fn(workout, fitness, recovery, weather_prompt)\n\n\n'
)
champion = body.replace(
    "def build_evening_prompt(", "def build_evening_prompt_champion(", 1)
challenger = body.replace(
    "def build_evening_prompt(", "def build_evening_prompt_challenger(", 1)

shutil.copyfile(P, P + ".bak")
new_src = src[:start] + wrapper + champion + "\n\n" + challenger + src[end:]
with open(P, "w", encoding="utf-8", newline="") as f:
    f.write(new_src)
print(f"OK: champion+challenger созданы, бэкап {P}.bak")
