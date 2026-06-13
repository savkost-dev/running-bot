"""Базовый график разбора тренировки: факт vs эталон (work и rest) — БЕЗ групп.

Самодостаточен: точки факта + эталон. Группы (s4_groups.draw_groups) подключаются
ОДНОЙ строкой под флагом WITH_GROUPS — базовый график от них не зависит.

Источники:
  - ФАКТ: Garmin activity splits (лэпы) → темпы work / rest по номеру.
  - ЭТАЛОН: Garmin workout по workoutId (план, по которому бежали).
    work: pace.zone с диапазоном → прогрессия от медленного края к быстрому
          по ходу интервалов (медленная граница 1-го → быстрая граница).
    rest: один плановый темп → коридор плановый ±15 с.
          Нет планового темпа отдыха (no.target) → коридор НЕ рисуем (правило данных).

Правило данных: никаких подстановок. Нет плана/темпа → элемент не рисуется.

Пишет в базу: НЕТ. Сохраняет PNG в /tmp. Импортирует: garmin, s4_groups. НЕ bot.py.

Запуск:
    venv/bin/python3 scripts/s4_plot.py                 # uid=2, последняя DD-активность
    venv/bin/python3 scripts/s4_plot.py 2 23219097987   # конкретная активность
"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import matplotlib
matplotlib.use("Agg")  # без дисплея, рендер в файл
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter

import garmin
from s4_groups import draw_groups

# ── ФЛАГ: показывать группы (зелёные средние + тренд через средние). Базовый график без них. ──
WITH_GROUPS = False
# ── ФЛАГ: тренд по ВСЕМ фактическим точкам (линейная регрессия, синий пунктир). ──
WITH_FACT_TREND = True
# ── ФЛАГ: дельты факт↔эталон (вертикальные отрезки + подписи разницы в сек). ──
WITH_DELTAS = True
DELTA_LABEL_MIN_SEC = 0  # подписывать дельту только если |разница| >= это (0 = все)

REST_CORRIDOR_SEC = 15  # коридор отдыха: зелёная зона = плановый темп ± это (сек/км)
REST_CORRIDOR_YELLOW = 30  # жёлтая зона = плановый темп ± это

# ── Пороги цвета отклонения (сек/км, по модулю). В ОДНОМ месте. ──
#   < GREEN → зелёный; GREEN..YELLOW → жёлтый; > YELLOW → красный
DELTA_GREEN = 5
DELTA_YELLOW = 10


def _delta_color(abs_sec):
    """Цвет по абсолютному отклонению (сек/км). Пороги — константы выше."""
    if abs_sec < DELTA_GREEN:
        return "green"
    if abs_sec < DELTA_YELLOW:
        return "gold"
    return "red"


def _draw_deltas(ax, x, y, etalon):
    """Вертикальный отрезок от факта к эталону, цвет по величине отклонения,
    подпись разницы (сек) слева по центру отрезка.
    Знак: + факт медленнее эталона, − быстрее. Отключается флагом WITH_DELTAS."""
    if not WITH_DELTAS or etalon is None:
        return
    for xi, yi, ei in zip(x, y, etalon):
        d = yi - ei  # сек/км: >0 факт медленнее
        color = _delta_color(abs(d))
        ax.plot([xi, xi], [yi, ei], color=color, linewidth=1.6, alpha=0.8, zorder=2)
        if abs(d) < DELTA_LABEL_MIN_SEC:
            continue
        sign = "+" if d > 0 else "−"
        mid = (yi + ei) / 2.0  # центр вертикали
        ax.annotate(f"{sign}{abs(int(round(d)))}", (xi, mid),
                    textcoords="offset points", xytext=(-6, 0),
                    ha="right", va="center", fontsize=7, color=color, zorder=6)


def _draw_fact_trend(ax, x, y):
    """Тренд по всем фактическим точкам (МНК). Отключается флагом WITH_FACT_TREND.
    Возвращает массив тренда (для инфо-бокса) или None."""
    if not WITH_FACT_TREND or len(x) < 2:
        return None
    from scipy import stats
    slope, intercept, *_ = stats.linregress(x, y)
    trend = slope * np.asarray(x) + intercept
    ax.plot(x, trend, "b--", linewidth=2.5, zorder=4,
            label=f"Тренд факта ({_pace_formatter(trend[0],0)} → {_pace_formatter(trend[-1],0)})")
    return trend


def _info_box(ax, fact_trend, etalon):
    """Вторая легенда — текстовый бокс слева вверху: мин/макс тренда факта и эталона."""
    lines = []
    if fact_trend is not None and len(fact_trend):
        lines += ["Тренд факта",
                  f"  Мин: {_pace_formatter(min(fact_trend),0)}/км",
                  f"  Макс: {_pace_formatter(max(fact_trend),0)}/км"]
    if etalon is not None and len(etalon):
        if lines:
            lines.append("")
        lines += ["Эталон",
                  f"  Мин: {_pace_formatter(min(etalon),0)}/км",
                  f"  Макс: {_pace_formatter(max(etalon),0)}/км"]
    if not lines:
        return
    ax.text(0.02, 0.98, "\n".join(lines), transform=ax.transAxes, fontsize=9,
            verticalalignment="top", horizontalalignment="left",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="white",
                      edgecolor="black", alpha=0.9), fontfamily="monospace")


def _pace_formatter(x, pos):
    if x <= 0:
        return ""
    return f"{int(x // 60)}:{int(x % 60):02d}"


def _ms_to_sec_per_km(v):
    """Скорость м/с → темп сек/км. None если нет/0."""
    if not v:
        return None
    return 1000.0 / v


# ── Сбор ФАКТА из splits ──
def _fact_paces(splits):
    """Возвращает (work_sec[], rest_sec[]) — темпы сек/км по номеру повтора.
    role: ACTIVE→work, RECOVERY→rest. tail (аномально короткий хвост) отбрасываем."""
    laps = []
    if isinstance(splits, dict):
        laps = splits.get("lapDTOs") or splits.get("laps") or []
    laps = [l for l in laps if isinstance(l, dict)]

    def pace(lp):
        d = lp.get("distance")
        t = lp.get("duration") or lp.get("movingDuration")
        return (t / (d / 1000)) if (d and t) else None

    work, rest = [], []
    for lp in laps:
        it = str(lp.get("intensityType") or "").upper()
        p = pace(lp)
        if p is None:
            continue
        if it == "ACTIVE":
            work.append((lp.get("distance"), p))
        elif it in ("RECOVERY", "REST"):
            rest.append(p)
    # tail: последний work аномально короткий (<50% медианы) — убираем
    if len(work) >= 3:
        import statistics
        med = statistics.median([d for d, _ in work if d])
        if work[-1][0] and work[-1][0] < 0.5 * med:
            work = work[:-1]
    return [p for _, p in work], rest


# ── Сбор ПЛАНА из Garmin workout ──
def _plan_targets(wkt):
    """Из workout достаёт целевые границы (сек/км) первого work-шага и rest-шага.
    Возвращает {work: (slow, fast) | None, rest: target | None}.
    Идём по шагам внутри repeat: первый interval = work, recovery = rest."""
    work_bounds = None
    rest_target = None

    def scan(steps):
        nonlocal work_bounds, rest_target
        for st in steps or []:
            if not isinstance(st, dict):
                continue
            if st.get("type") == "RepeatGroupDTO":
                scan(st.get("workoutSteps"))
                continue
            stype = (st.get("stepType") or {}).get("stepTypeKey")
            ttype = (st.get("targetType") or {}).get("workoutTargetTypeKey")
            t1 = _ms_to_sec_per_km(st.get("targetValueOne"))
            t2 = _ms_to_sec_per_km(st.get("targetValueTwo"))
            if stype == "interval" and ttype == "pace.zone" and work_bounds is None:
                if t1 and t2:
                    # t1/t2 — границы в сек/км; slow = больше секунд, fast = меньше
                    work_bounds = (max(t1, t2), min(t1, t2))
            elif stype == "recovery" and rest_target is None:
                if ttype == "pace.zone" and t1 and t2:
                    rest_target = (t1 + t2) / 2.0  # один темп (часто t1==t2)

    for seg in (wkt.get("workoutSegments") or []):
        scan(seg.get("workoutSteps"))
    return {"work": work_bounds, "rest": rest_target}


# ── Построение графика work ──
def plot_work(work_sec, plan_work, title, out_path):
    n = len(work_sec)
    if n == 0:
        print("work: нет данных факта — график не строим")
        return
    x = np.arange(1, n + 1)
    y = np.array(work_sec)

    fig, ax = plt.subplots(figsize=(14, 8))
    ax.scatter(x, y, color="blue", s=80, zorder=3, label="Все интервалы")

    # Эталон: прогрессия от медленной границы к быстрой по ходу интервалов
    etalon = None
    if plan_work:
        slow, fast = plan_work
        etalon = np.linspace(slow, fast, n)
        ax.plot(x, etalon, color="red", linewidth=4, zorder=4, alpha=0.8,
                label=f"Эталон ({_pace_formatter(slow,0)} → {_pace_formatter(fast,0)})")
        _draw_deltas(ax, x, y, etalon)

    fact_trend = _draw_fact_trend(ax, x, y)

    if WITH_GROUPS:
        draw_groups(ax, x, y)

    # Вторая легенда (текстовый бокс): мин/макс тренда факта и эталона
    _info_box(ax, fact_trend, etalon)

    ax.invert_yaxis()
    ax.set_xlabel("Номер интервала", fontsize=12)
    ax.set_ylabel("Темп (мин:сек/км)", fontsize=12)
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.yaxis.set_major_formatter(FuncFormatter(_pace_formatter))
    ax.set_xticks(x)
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(loc="lower right", fontsize=10)
    plt.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    print(f"work: сохранён {out_path}")


# ── Построение графика rest ──
def plot_rest(rest_sec, plan_rest, title, out_path):
    n = len(rest_sec)
    if n == 0:
        print("rest: нет данных факта — график не строим")
        return
    x = np.arange(1, n + 1)
    y = np.array(rest_sec)

    fig, ax = plt.subplots(figsize=(14, 8))

    # Двойной коридор отдыха (прозрачность 30%): зелёная ±15, жёлтая ±30.
    # Нет плана → коридор НЕ рисуем (правило данных).
    if plan_rest:
        g_lo, g_hi = plan_rest - REST_CORRIDOR_SEC, plan_rest + REST_CORRIDOR_SEC
        y_lo, y_hi = plan_rest - REST_CORRIDOR_YELLOW, plan_rest + REST_CORRIDOR_YELLOW
        # жёлтая зона (вся полоса ±30) под зелёной
        ax.fill_between(x, y_lo, y_hi, color="gold", alpha=0.25, zorder=1,
                        label=f"Жёлтая ±{REST_CORRIDOR_YELLOW}с ({_pace_formatter(y_lo,0)}–{_pace_formatter(y_hi,0)})")
        # зелёная зона (±15) поверх
        ax.fill_between(x, g_lo, g_hi, color="green", alpha=0.25, zorder=1,
                        label=f"Зелёная ±{REST_CORRIDOR_SEC}с ({_pace_formatter(g_lo,0)}–{_pace_formatter(g_hi,0)})")
        # цвет точек по зоне
        colors = []
        for v in y:
            if g_lo <= v <= g_hi:
                colors.append("green")
            elif y_lo <= v <= y_hi:
                colors.append("gold")
            else:
                colors.append("red")
        ax.scatter(x, y, color=colors, s=80, zorder=3, label="Отдых")
    else:
        ax.scatter(x, y, color="blue", s=80, zorder=3, label="Отдых")

    _draw_fact_trend(ax, x, y)

    if WITH_GROUPS:
        draw_groups(ax, x, y)

    ax.invert_yaxis()
    ax.set_xlabel("Номер интервала", fontsize=12)
    ax.set_ylabel("Темп (мин:сек/км)", fontsize=12)
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.yaxis.set_major_formatter(FuncFormatter(_pace_formatter))
    ax.set_xticks(x)
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(loc="upper right", fontsize=10)
    plt.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    print(f"rest: сохранён {out_path}")


async def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    forced_id = int(sys.argv[2]) if len(sys.argv) > 2 else None

    client = await garmin._client(uid)
    if not client:
        print(f"user={uid}: нет клиента Garmin")
        return

    if forced_id:
        act = await asyncio.to_thread(client.get_activity, forced_id)
    else:
        acts = await asyncio.to_thread(client.get_activities, 0, 20)
        runs = [a for a in (acts or [])
                if "running" in str((a.get("activityType") or {}).get("typeKey", ""))
                and "DD_" in str(a.get("activityName") or "")]
        if not runs:
            print("Беговой активности с маской DD_ в последних 20 нет")
            return
        act = runs[0]

    act_id = act.get("activityId")
    name = act.get("activityName")
    wkt_id = act.get("workoutId")
    print(f"Активность: {name!r}  activityId={act_id}  workoutId={wkt_id}")

    splits = await asyncio.to_thread(client.get_activity_splits, act_id)
    work_sec, rest_sec = _fact_paces(splits)
    print(f"Факт: work={len(work_sec)} отрезков, rest={len(rest_sec)}")

    # План из Garmin workout (без фолбэка на БД — по договорённости)
    plan = {"work": None, "rest": None}
    if wkt_id:
        try:
            wkt = await asyncio.to_thread(client.get_workout_by_id, wkt_id)
            plan = _plan_targets(wkt)
        except Exception as e:
            print(f"План workout {wkt_id} недоступен: {type(e).__name__}: {e}")
    else:
        print("workoutId пуст — эталон/коридор не рисуем (фолбэк не трогаем)")
    print(f"План: work={plan['work']}  rest={plan['rest']}")

    plot_work(work_sec, plan["work"], "Анализ интервальной тренировки",
              "/tmp/s4_work.png")
    plot_rest(rest_sec, plan["rest"], "Анализ восстановительных интервалов",
              "/tmp/s4_rest.png")
    print("\nГотово. Забери картинки:")
    print("  scp -i ... root@167.172.185.88:/tmp/s4_work.png .")
    print("  scp -i ... root@167.172.185.88:/tmp/s4_rest.png .")


if __name__ == "__main__":
    asyncio.run(main())
