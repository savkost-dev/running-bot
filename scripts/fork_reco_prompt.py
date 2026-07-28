"""Развилка БОЕВОГО промта рекомендации (B, зонный) — champion/challenger.

Делает всё сам:
  1. Если в файле осталась мёртвая legacy-развилка (build_evening_prompt_champion) —
     восстанавливает claude_advisor.py из .bak (чистое состояние) и продолжает.
  2. Находит функцию, собирающую боевой промт, по маркеру «Подбери группу для бегуна».
  3. Форкает её: <имя>_champion (боевое тело), <имя>_challenger (копия для правок),
     под старым именем — обёртка с константой RECO_PROMPT_VARIANT ("challenger"/"champion").
Вызовы менять не нужно. Идемпотентен. Запуск: python scripts/fork_reco_prompt.py
"""
import shutil

P = r"D:\running-bot\src\claude_advisor.py"
BAK = P + ".bak"
MARK = "Подбери группу для бегуна"

with open(P, encoding="utf-8") as f:
    src = f.read()

# 1. Чистим след неудачной legacy-развилки
if "build_evening_prompt_champion" in src:
    with open(BAK, encoding="utf-8") as f:
        bak = f.read()
    if "build_evening_prompt_champion" in bak or MARK not in bak:
        print("ОШИБКА: .bak тоже нечистый — вручную разберёмся, ничего не трогаю")
        raise SystemExit(1)
    src = bak
    print("Откат из .bak выполнен (legacy-развилка убрана).")

if "_reco_champion" in src or "RECO_PROMPT_VARIANT" in src:
    print("Развилка боевого промта уже сделана — выходим.")
    raise SystemExit(0)

# 2. Находим функцию по маркеру
mark_pos = src.index(MARK)
fn_start = src.rindex("\ndef ", 0, mark_pos) + 1
sig_end = src.index(":\n", fn_start)
sig = src[fn_start:sig_end + 1]
fn_name = sig.split("(")[0].replace("def ", "").strip()
body_end = src.index("\ndef ", fn_start + 4)
body = src[fn_start:body_end]
print(f"Найдена функция: {sig.strip()[:120]}")

# 3. Форк
args_sig = sig[sig.index("("):]  # "(...):" — параметры как есть
call_args = ", ".join(
    a.split(":")[0].split("=")[0].strip()
    for a in sig[sig.index("(") + 1:sig.rindex(")")].split(",") if a.strip())
wrapper = (
    f'RECO_PROMPT_VARIANT = "challenger"  # "champion" — мгновенный откат к боевой версии\n\n\n'
    f'def {fn_name}{args_sig}\n'
    f'    """Развилка боевого промта рекомендации: challenger правится, champion заморожен."""\n'
    f'    fn = ({fn_name}_reco_challenger if RECO_PROMPT_VARIANT == "challenger"\n'
    f'          else {fn_name}_reco_champion)\n'
    f'    return fn({call_args})\n\n\n'
)
champion = body.replace(f"def {fn_name}(", f"def {fn_name}_reco_champion(", 1)
challenger = body.replace(f"def {fn_name}(", f"def {fn_name}_reco_challenger(", 1)

shutil.copyfile(P, P + ".bak2")
new_src = src[:fn_start] + wrapper + champion + "\n\n" + challenger + src[body_end:]
with open(P, "w", encoding="utf-8", newline="") as f:
    f.write(new_src)
print(f"OK: {fn_name}_reco_champion + {fn_name}_reco_challenger созданы, бэкап .bak2")
