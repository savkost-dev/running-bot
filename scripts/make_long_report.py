"""make_long_report.py — разовый (20.09.2026): выносит разбор лонга из src/ai_package.py в src/long_report.py.

Решение Антона: лонг изолирован от интервалов всем, кроме поиска активности; когда правка задевает
общий код — решаем, оставить ли его общим, иначе копия. Этот скрипт:
  - ПЕРЕНОСИТ build_long_package и строку PROMPT_LONG (из ai_package.py удаляются);
  - КОПИРУЕТ _plan_text (в ai_package.py остаётся для интервалов);
  - недостающие имена (поиск активности, форматирование, db, ar …) импортирует из ai_package.
Точный текст функций берётся из файла (ast), ничего не переписывается руками.

Запуск локально из D:\\running-bot:
  python scripts\\make_long_report.py           — только показать, что будет сделано
  python scripts\\make_long_report.py --apply   — сделать
bot.py не импортирует.
"""
import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "ai_package.py"
DST = ROOT / "src" / "long_report.py"
MOVE = ["build_long_package", "PROMPT_LONG"]   # удаляются из ai_package.py
COPY = ["_plan_text"]                           # остаются в ai_package.py


def main(apply):
    if DST.exists():
        print(f"СТОП: {DST} уже существует — ничего не делаю")
        return
    text = SRC.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)

    nodes, defined = {}, set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nodes[node.name] = node
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    nodes.setdefault(t.id, node)
                    defined.add(t.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])

    missing = [n for n in MOVE + COPY if n not in nodes]
    if missing:
        print(f"СТОП: не найдено в ai_package.py: {missing}")
        return

    def seg(node):
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
        return start, node.end_lineno

    taken = MOVE + COPY
    free = set()
    for n in taken:
        for sub in ast.walk(nodes[n]):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                free.add(sub.id)
    need = sorted(n for n in free - set(dir(builtins)) - set(taken) if n in defined)

    print("Импорт из ai_package:", ", ".join(need))
    for n in taken:
        a, b = seg(nodes[n])
        print(f"  {'ПЕРЕНОС' if n in MOVE else 'КОПИЯ  '} {n}: строки {a + 1}–{b} ({b - a} стр.)")
    if not apply:
        print("\nЭто просмотр. Для выполнения: python scripts\\make_long_report.py --apply")
        return

    body = []
    for n in COPY + MOVE:   # сначала копии (вспомогательные), потом перенесённое
        a, b = seg(nodes[n])
        body.append("".join(lines[a:b]).rstrip() + "\n")
    header = (
        '"""long_report.py — разбор ЛОНГА (DDLong-…), с 20.09.2026.\n'
        "Изолирован от интервалов (ai_package.py) всем, кроме поиска активности: правки лонга — только здесь.\n"
        "Создан scripts/make_long_report.py: build_long_package и PROMPT_LONG перенесены, _plan_text скопирован.\n"
        '"""\n'
        "from ai_package import (\n" + "".join(f"    {n},\n" for n in need) + ")\n\n\n"
    )
    DST.write_text(header + "\n\n".join(body), encoding="utf-8")

    cut = sorted((seg(nodes[n]) for n in MOVE), reverse=True)
    for a, b in cut:
        del lines[a:b]
    SRC.write_text("".join(lines), encoding="utf-8")
    print(f"\nГотово: создан {DST.name}, из {SRC.name} удалено: {', '.join(MOVE)}")


if __name__ == "__main__":
    main("--apply" in sys.argv)
