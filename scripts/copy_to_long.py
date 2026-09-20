"""copy_to_long.py — копирует функции из src/ai_package.py (интервалы) в src/ai_package_long.py (лонг).

Правило Антона (20.09.2026): когда правка лонга задевает общий с интервалами код — решаем, оставить ли
его общим; если нет — копия для лонга. Этот скрипт делает копию точно (текст берётся из файла через ast):
  - дописывает функции в конец ai_package_long.py (в ai_package.py они остаются для интервалов);
  - убирает их имена из блока «from ai_package import (...)» в ai_package_long.py;
  - добавляет в этот блок имена из ai_package, которые нужны копиям и ещё не подключены.
Если функция уже есть в ai_package_long.py — СТОП.

Запуск локально из D:\\running-bot:
  python scripts\\copy_to_long.py _template_json            — только показать
  python scripts\\copy_to_long.py _template_json --apply    — сделать
Потом проверка:  python -m pyflakes src\\ai_package_long.py
bot.py не импортирует.
"""
import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "ai_package.py"
DST = ROOT / "src" / "ai_package_long.py"


def _top(tree):
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
    return nodes, defined


def main(names, apply):
    src_text = SRC.read_text(encoding="utf-8")
    src_lines = src_text.splitlines(keepends=True)
    src_nodes, src_defined = _top(ast.parse(src_text))
    dst_text = DST.read_text(encoding="utf-8")
    dst_tree = ast.parse(dst_text)
    dst_nodes, _ = _top(dst_tree)

    bad = [n for n in names if n not in src_nodes]
    if bad:
        print(f"СТОП: нет в ai_package.py: {bad}")
        return
    dup = [n for n in names if n in dst_nodes]
    if dup:
        print(f"СТОП: уже есть в ai_package_long.py: {dup}")
        return

    imp = next((n for n in dst_tree.body
                if isinstance(n, ast.ImportFrom) and n.module == "ai_package"), None)
    if imp is None:
        print("СТОП: в ai_package_long.py нет блока «from ai_package import (...)»")
        return
    imported = {a.asname or a.name for a in imp.names}

    free = set()
    for n in names:
        for sub in ast.walk(src_nodes[n]):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                free.add(sub.id)
    add = sorted(n for n in free - set(dir(builtins)) - set(names) - set(dst_nodes) - imported
                 if n in src_defined)
    drop = sorted(n for n in names if n in imported)

    for n in names:
        nd = src_nodes[n]
        print(f"КОПИЯ {n}: строки {nd.lineno}–{nd.end_lineno} ai_package.py")
    print("Убрать из импорта:", ", ".join(drop) or "—")
    print("Добавить в импорт:", ", ".join(add) or "—")
    if not apply:
        print("\nЭто просмотр. Для выполнения добавь --apply")
        return

    dst_lines = dst_text.splitlines(keepends=True)
    names_now = sorted((imported - set(drop)) | set(add))
    block = "from ai_package import (\n" + "".join(f"    {n},\n" for n in names_now) + ")\n"
    dst_lines[imp.lineno - 1:imp.end_lineno] = [block]
    body = []
    for n in names:
        nd = src_nodes[n]
        start = min([nd.lineno] + [d.lineno for d in getattr(nd, "decorator_list", [])]) - 1
        body.append("".join(src_lines[start:nd.end_lineno]).rstrip() + "\n")
    out = "".join(dst_lines).rstrip() + "\n\n\n" + "\n\n".join(body)
    DST.write_text(out, encoding="utf-8")
    print(f"\nГотово: в {DST.name} добавлено: {', '.join(names)}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--apply"]
    if not args:
        print("Укажи имена функций: python scripts\\copy_to_long.py _template_json [--apply]")
    else:
        main(args, "--apply" in sys.argv)
