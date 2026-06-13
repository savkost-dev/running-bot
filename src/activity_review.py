"""Разбор выполненной тренировки (s4_activity) → графики факт vs план.

Используется командой /last в боте. Берёт последнюю беговую активность Garmin
с маской DD_, факт из splits, план из Garmin workout по workoutId, строит
два PNG (work и rest) и возвращает пути к файлам.

Рабочие ветки бота НЕ затрагивает. Источник эталона — только Garmin workout
(фолбэк на БД не реализован сознательно).

Зависимости: garmin (клиент), matplotlib, numpy, scipy. НЕ импортирует bot.py.
"""
import os
import asyncio
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter

import garmin

# ── Флаги отображения (по умолчанию: базовый график + тренд факта + дельты) ──
WITH_GROUPS = False          # зелёные средние по группам + тренд через средние
WITH_FACT_TREND = True       # тренд по всем фактическим точкам (синий пунктир)
WITH_DELTAS = True           # дельты факт↔эталон (цветные отрезки + подписи)
DELTA_LABEL_MIN_SEC = 0      # подписывать дельту, если |разница| >= это (0 = все)

REST_CORRIDOR_SEC = 15       # коридор отдыха: зелёная зона = плановый темп ± это
REST_CORRIDOR_YELLOW = 30    # жёлтая зона = плановый темп ± это

# Пороги цвета отклонения (сек/км, по модулю). В одном месте.
DELTA_GREEN = 5
DELTA_YELLOW = 10


def _pace_formatter(x, pos=None):
    if x <= 0:
        return ""
    return f"{int(x // 60)}:{int(x % 60):02d}"


def _ms_to_sec_per_km(v):
    if not v:
        return None
    return 1000.0 / v


def _delta_color(abs_sec):
    if abs_sec < DELTA_GREEN:
        return "green"
    if abs_sec < DELTA_YELLOW:
        return "gold"
    return "red"


# ── Сбор факта ──
def _fact_paces(splits):
    """(work_sec[], rest_sec[]) — темпы сек/км. tail (короткий хвост) отбрасываем."""
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
    if len(work) >= 3:
        med = statistics.median([d for d, _ in work if d])
        if work[-1][0] and work[-1][0] < 0.5 * med:
            work = work[:-1]
    return [p for _, p in work], rest


# ── Сбор плана из Garmin workout ──
def _plan_targets(wkt):
    """{work: (slow, fast) | None, rest: target | None} в сек/км."""
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
                    work_bounds = (max(t1, t2), min(t1, t2))
            elif stype == "recovery" and rest_target is None:
                if ttype == "pace.zone" and t1 and t2:
                    rest_target = (t1 + t2) / 2.0

    for seg in (wkt.get("workoutSegments") or []):
        scan(seg.get("workoutSteps"))
    return {"work": work_bounds, "rest": rest_target}


def _draw_deltas(ax, x, y, etalon):
    if not WITH_DELTAS or etalon is None:
        return
    for xi, yi, ei in zip(x, y, etalon):
        d = yi - ei
        color = _delta_color(abs(d))
        ax.plot([xi, xi], [yi, ei], color=color, linewidth=1.6, alpha=0.8, zorder=2)
        if abs(d) < DELTA_LABEL_MIN_SEC:
            continue
        sign = "+" if d > 0 else "−"
        mid = (yi + ei) / 2.0
        ax.annotate(f"{sign}{abs(int(round(d)))}", (xi, mid),
                    textcoords="offset points", xytext=(-6, 0),
                    ha="right", va="center", fontsize=7, color=color, zorder=6)


def _draw_fact_trend(ax, x, y):
    if not WITH_FACT_TREND or len(x) < 2:
        return None
    from scipy import stats
    slope, intercept, *_ = stats.linregress(x, y)
    trend = slope * np.asarray(x) + intercept
    ax.plot(x, trend, "b--", linewidth=2.5, zorder=4,
            label=f"Тренд факта ({_pace_formatter(trend[0])} → {_pace_formatter(trend[-1])})")
    return trend


def _info_box(ax, fact_trend, etalon):
    lines = []
    if fact_trend is not None and len(fact_trend):
        lines += ["Тренд факта",
                  f"  Мин: {_pace_formatter(min(fact_trend))}/км",
                  f"  Макс: {_pace_formatter(max(fact_trend))}/км"]
    if etalon is not None and len(etalon):
        if lines:
            lines.append("")
        lines += ["Эталон",
                  f"  Мин: {_pace_formatter(min(etalon))}/км",
                  f"  Макс: {_pace_formatter(max(etalon))}/км"]
    if not lines:
        return
    ax.text(0.02, 0.98, "\n".join(lines), transform=ax.transAxes, fontsize=9,
            verticalalignment="top", horizontalalignment="left",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="white",
                      edgecolor="black", alpha=0.9), fontfamily="monospace")


def _plot_work(work_sec, plan_work, title, out_path):
    n = len(work_sec)
    if n == 0:
        return None
    x = np.arange(1, n + 1)
    y = np.array(work_sec)
    fig, ax = plt.subplots(figsize=(14, 8))
    ax.scatter(x, y, color="blue", s=80, zorder=3, label="Все интервалы")
    etalon = None
    if plan_work:
        slow, fast = plan_work
        etalon = np.linspace(slow, fast, n)
        ax.plot(x, etalon, color="red", linewidth=4, zorder=4, alpha=0.8,
                label=f"Эталон ({_pace_formatter(slow)} → {_pace_formatter(fast)})")
        _draw_deltas(ax, x, y, etalon)
    fact_trend = _draw_fact_trend(ax, x, y)
    if WITH_GROUPS:
        from s4_groups import draw_groups
        draw_groups(ax, x, y)
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
    return out_path


def _plot_rest(rest_sec, plan_rest, title, out_path):
    n = len(rest_sec)
    if n == 0:
        return None
    x = np.arange(1, n + 1)
    y = np.array(rest_sec)
    fig, ax = plt.subplots(figsize=(14, 8))
    if plan_rest:
        g_lo, g_hi = plan_rest - REST_CORRIDOR_SEC, plan_rest + REST_CORRIDOR_SEC
        y_lo, y_hi = plan_rest - REST_CORRIDOR_YELLOW, plan_rest + REST_CORRIDOR_YELLOW
        ax.fill_between(x, y_lo, y_hi, color="gold", alpha=0.2, zorder=1,
                        label=f"Жёлтая ±{REST_CORRIDOR_YELLOW}с ({_pace_formatter(y_lo)}–{_pace_formatter(y_hi)})")
        ax.fill_between(x, g_lo, g_hi, color="green", alpha=0.2, zorder=1,
                        label=f"Зелёная ±{REST_CORRIDOR_SEC}с ({_pace_formatter(g_lo)}–{_pace_formatter(g_hi)})")
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
        from s4_groups import draw_groups
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
    return out_path


async def build_review(db_user_id: int, out_dir: str = "/tmp") -> dict:
    """Главная точка: собирает факт+план последней DD-активности, строит графики.

    Возвращает {ok, name, work_png, rest_png, msg}. Любой PNG может быть None.
    Рабочие ветки не трогает.
    """
    client = await garmin._client(db_user_id)
    if not client:
        return {"ok": False, "msg": "Garmin не подключён или нет клиента."}

    acts = await asyncio.to_thread(client.get_activities, 0, 20)
    runs = [a for a in (acts or [])
            if "running" in str((a.get("activityType") or {}).get("typeKey", ""))
            and "DD_" in str(a.get("activityName") or "")]
    if not runs:
        return {"ok": False, "msg": "Нет беговой тренировки с маской DD_ в последних 20."}

    act = runs[0]
    act_id = act.get("activityId")
    name = act.get("activityName")
    wkt_id = act.get("workoutId")

    splits = await asyncio.to_thread(client.get_activity_splits, act_id)
    work_sec, rest_sec = _fact_paces(splits)

    plan = {"work": None, "rest": None}
    if wkt_id:
        try:
            wkt = await asyncio.to_thread(client.get_workout_by_id, wkt_id)
            plan = _plan_targets(wkt)
        except Exception as e:
            print(f"build_review: план {wkt_id} недоступен: {type(e).__name__}: {e}")

    base = os.path.join(out_dir, f"s4_{db_user_id}_{act_id}")
    work_png = await asyncio.to_thread(
        _plot_work, work_sec, plan["work"], "Анализ интервальной тренировки",
        base + "_work.png")
    rest_png = await asyncio.to_thread(
        _plot_rest, rest_sec, plan["rest"], "Анализ восстановительных интервалов",
        base + "_rest.png")

    return {"ok": True, "name": name, "work_png": work_png, "rest_png": rest_png,
            "n_work": len(work_sec), "n_rest": len(rest_sec), "msg": ""}
