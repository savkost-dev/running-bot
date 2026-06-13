"""Группировка отрезков для анализа тренировки — ОТДЕЛЬНЫЙ модуль.

Содержит ТОЛЬКО логику разбиения на группы и их отрисовку. Базовый график
(точки факта + эталон) от этого модуля не зависит и работает без него.
Подключается в график одной строкой под флагом.

Правило групп (согласовано 13.06.2026):
  - N < 8 отрезков → НЕ бить на группы вообще (вернуть []).
  - N >= 8: групп >= 4, размер группы 2..5, группы по возможности равные.
    Берём минимальное g>=4, при котором base=N//g <= 5 и (base+1)<=5 при остатке.
    Остаток r раскидываем +1 на первые r групп.
  Проверка: 8→[2,2,2,2], 12→[3,3,3,3], 15→[4,4,4,3], 20→[5,5,5,5],
            25→[5,5,5,5,5], 13→[4,3,3,3], 22→[5,5,4,4,4].

Запуск как самопроверка (без бота, без БД):
    venv/bin/python3 scripts/s4_groups.py
"""
from __future__ import annotations


def compute_groups(n: int) -> list[tuple[int, int]]:
    """Возвращает список (start_idx, end_idx) 1-based включительно по отрезкам.
    Пустой список, если группировка не применяется (N < 8)."""
    if n < 8:
        return []
    g = 4
    while True:
        base = n // g
        r = n % g
        # base в допустимом диапазоне и +1 (для остатка) не превышает 5
        if 2 <= base <= 5 and (r == 0 or base + 1 <= 5):
            break
        g += 1
        if g > n:  # страховка от зацикливания
            return []
    sizes = [base + 1 if i < r else base for i in range(g)]
    bounds = []
    pos = 1
    for s in sizes:
        bounds.append((pos, pos + s - 1))
        pos += s
    return bounds


def group_stats(values_sec, bounds):
    """По границам и значениям (секунды/км) считает (center_x, avg) для каждой группы."""
    import statistics
    out = []
    for (start, end) in bounds:
        chunk = values_sec[start - 1:end]
        if not chunk:
            continue
        center = (start + end) / 2.0
        out.append((center, statistics.mean(chunk), start, end))
    return out


def draw_groups(ax, intervals, values_sec, *, with_trend=True):
    """Рисует зелёные средние по группам, жёлтые звёзды-центры и тренд через средние.
    intervals — массив x (1..N), values_sec — темпы в сек/км.
    Если групп нет (N<8) — ничего не рисует и возвращает None."""
    import numpy as np
    from scipy import stats

    bounds = compute_groups(len(intervals))
    if not bounds:
        return None

    gs = group_stats(list(values_sec), bounds)
    centers = np.array([c for c, *_ in gs])
    avgs = np.array([a for _, a, *_ in gs])

    for i, (c, a, start, end) in enumerate(gs):
        ax.hlines(y=a, xmin=start, xmax=end, color="green", linewidth=2.5,
                  alpha=0.7, zorder=2, label="Среднее по группам" if i == 0 else "")
    ax.scatter(centers, avgs, color="yellow", s=300, zorder=5, marker="*",
               edgecolors="orange", linewidths=2.5, label="Центры групп")

    trend = None
    if with_trend and len(centers) >= 2:
        slope, intercept, *_ = stats.linregress(centers, avgs)
        trend = slope * np.asarray(intervals) + intercept
        ax.plot(intervals, trend, "r--", linewidth=2.5, zorder=4,
                label="Тренд через средние")
    return {"bounds": bounds, "centers": centers, "avgs": avgs, "trend": trend}


if __name__ == "__main__":
    # Самопроверка алгоритма на согласованных числах
    expected = {
        8: [2, 2, 2, 2], 12: [3, 3, 3, 3], 15: [4, 4, 4, 3],
        20: [5, 5, 5, 5], 25: [5, 5, 5, 5, 5], 13: [4, 3, 3, 3],
        22: [5, 5, 4, 4, 4], 6: [], 7: [],
    }
    ok = True
    for n, exp in expected.items():
        bounds = compute_groups(n)
        sizes = [e - s + 1 for s, e in bounds]
        mark = "OK" if sizes == exp else "FAIL"
        if sizes != exp:
            ok = False
        print(f"  N={n:>2}: {sizes}  ожидалось {exp}  [{mark}]")
    print("Все тесты пройдены." if ok else "ЕСТЬ РАСХОЖДЕНИЯ!")
