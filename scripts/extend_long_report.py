"""extend_long_report.py — разовый (20.09.2026): src/long_report.py → src/ai_package_long.py + копии.

Решение Антона: лонг изолирован от интервалов всем, кроме поиска активности и чтения данных
(разметка кругов и графики у лонга будут своими). Скрипт собирает src/ai_package_long.py заново:
  - всё, что уже лежит в src/long_report.py (build_long_package, PROMPT_LONG, копия _plan_text);
  - КОПИИ из src/ai_package.py (там остаются для интервалов): сбор кандидатов с разметкой кругов,
    разметка, расчёт кругов, графики, карточка — список COPY ниже;
  - остальные нужные имена импортирует из ai_package (поиск по маске, чтение данных, форматирование);
  - src/long_report.py удаляет.
Точный текст функций берётся из файлов (ast), руками ничего не переписывается.

Запуск локально из D:\\running-bot:
  python scripts\\extend_long_report.py           — только показать
  python scripts\\extend_long_report.py --apply   — сделать
bot.py не импортирует.
"""
import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "ai_package.py"
OLD = ROOT / "src" / "long_report.py"
DST = ROOT / "src" / "ai_package_long.py"
COPY = [
    "_garmin_candidate", "_strava_candidate", "_coros_candidate",          # сбор кандидата (с разметкой)
    "_drop_extra_first_lap", "_assign_button_laps", "_apply_plan_distances",  # разметка кругов
    "_enrich_laps", "_splits_200", "_hr_before",                           # расчёт кругов
    "build_charts", "build_charts_stacked", "_series_model", "_plan_diagram",  # графики
    "build_report_card", "_km_label",                                      # карточка
]


def _top(tree):
    """{имя: узел} верхнего уровня и множество всех имён, определённых/импортированных на верхнем уровне."""
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


def _seg(lines, node):
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
    return "".join(lines[start:node.end_lineno]).rstrip() + "\n"


def main(apply):
    if DST.exists():
        print(f"СТОП: {DST.name} уже существует — ничего не делаю")
        return
    if not OLD.exists():
        print(f"СТОП: нет {OLD.name}")
        return
    src_text = SRC.read_text(encoding="utf-8")
    src_lines = src_text.splitlines(keepends=True)
    src_nodes, src_defined = _top(ast.parse(src_text))
    old_text = OLD.read_text(encoding="utf-8")
    old_lines = old_text.splitlines(keepends=True)
    old_nodes, _ = _top(ast.parse(old_text))
    old_names = [n for n in old_nodes]   # функции/присваивания из long_report.py (без импортов)

    missing = [n for n in COPY if n not in src_nodes]
    if missing:
        print(f"СТОП: не найдено в ai_package.py: {missing}")
        return

    own = set(COPY) | set(old_names)
    free = set()
    for node in [src_nodes[n] for n in COPY] + [old_nodes[n] for n in old_names]:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                free.add(sub.id)
    need = sorted(n for n in free - set(dir(builtins)) - own if n in src_defined)

    print("Из long_report.py:", ", ".join(old_names))
    print("Копии из ai_package.py:")
    for n in COPY:
        nd = src_nodes[n]
        print(f"  {n}: строки {nd.lineno}–{nd.end_lineno} ({nd.end_lineno - nd.lineno + 1} стр.)")
    print("Импорт из ai_package:", ", ".join(need))
    if not apply:
        print("\nЭто просмотр. Для выполнения: python scripts\\extend_long_report.py --apply")
        return

    header = (
        '"""ai_package_long.py — разбор ЛОНГА (DDLong-…), с 20.09.2026. Пара к ai_package.py (интервалы).\n'
        "Общее с интервалами — только поиск активности по маске и чтение данных (импорт из ai_package);\n"
        "всё остальное (разметка кругов, план, пакет, промт, графики, карточка) — своё: правки лонга только здесь.\n"
        "Собран scripts/make_long_report.py + scripts/extend_long_report.py.\n"
        '"""\n'
        "from ai_package import (\n" + "".join(f"    {n},\n" for n in need) + ")\n\n\n"
    )
    parts = [_seg(src_lines, src_nodes[n]) for n in COPY]
    parts += [_seg(old_lines, old_nodes[n]) for n in old_names]
    DST.write_text(header + "\n\n".join(parts), encoding="utf-8")
    OLD.unlink()
    print(f"\nГотово: создан {DST.name}, {OLD.name} удалён. ai_package.py не менялся.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
