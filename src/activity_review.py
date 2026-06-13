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

# ── Ночной (тёмный) режим всех картинок. По умолчанию тёмный; build_review
#    выставляет его на время вызова из аргумента команды (/last light → светлый). ──
DARK_MODE = True


def _theme():
    """Цвета темы в одном месте. Базовые цвета данных (синий/красный/зелёный/жёлтый)
    одинаковы в обеих темах — на тёмном читаются. Меняются фон, текст, сетка,
    цвет точек факта и фон вспомогательных боксов."""
    if DARK_MODE:
        return {
            "fact": "#4da6ff",        # точки факта (светло-синий, ярче на тёмном)
            "box_face": "#222222",    # фон текст-бокса / шапки таблицы
            "box_edge": "#bbbbbb",
            "text": "#e6e6e6",        # основной текст в боксах
        }
    return {
        "fact": "blue",
        "box_face": "white",
        "box_edge": "black",
        "text": "black",
    }


def _rc():
    """matplotlib rcParams для текущей темы (для rc_context)."""
    if DARK_MODE:
        fg, bg = "#e6e6e6", "#1a1a1a"
        return {
            "figure.facecolor": bg, "axes.facecolor": bg, "savefig.facecolor": bg,
            "text.color": fg, "axes.labelcolor": fg, "axes.edgecolor": fg,
            "xtick.color": fg, "ytick.color": fg, "axes.titlecolor": fg,
            "grid.color": "#555555", "legend.facecolor": "#222222",
            "legend.edgecolor": "#777777", "legend.labelcolor": fg,
        }
    return {}


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
def _fact_laps(splits):
    """(work[], rest[]) где каждый элемент = (duration_s, pace_sec_per_km).
    tail (аномально короткий последний work) отбрасываем."""
    laps = []
    if isinstance(splits, dict):
        laps = splits.get("lapDTOs") or splits.get("laps") or []
    laps = [l for l in laps if isinstance(l, dict)]

    work, rest = [], []  # элементы: (distance, duration_s, pace)
    for lp in laps:
        it = str(lp.get("intensityType") or "").upper()
        d = lp.get("distance")
        t = lp.get("duration") or lp.get("movingDuration")
        p = (t / (d / 1000)) if (d and t) else None
        if p is None:
            continue
        if it == "ACTIVE":
            work.append((d, t, p))
        elif it in ("RECOVERY", "REST"):
            rest.append((d, t, p))
    if len(work) >= 3:
        med = statistics.median([d for d, _, _ in work if d])
        if work[-1][0] and work[-1][0] < 0.5 * med:
            work = work[:-1]
    return ([(t, p) for _, t, p in work], [(t, p) for _, t, p in rest])


def _fact_paces(splits):
    """(work_sec[], rest_sec[]) — только темпы сек/км (обёртка над _fact_laps)."""
    work, rest = _fact_laps(splits)
    return [p for _, p in work], [p for _, p in rest]


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
    th = _theme()
    ax.text(0.02, 0.98, "\n".join(lines), transform=ax.transAxes, fontsize=9,
            verticalalignment="top", horizontalalignment="left", color=th["text"],
            bbox=dict(boxstyle="round,pad=0.5", facecolor=th["box_face"],
                      edgecolor=th["box_edge"], alpha=0.9), fontfamily="monospace")


def _plot_work(work_sec, plan_work, title, out_path):
    n = len(work_sec)
    if n == 0:
        return None
    x = np.arange(1, n + 1)
    y = np.array(work_sec)
    th = _theme()
    with plt.rc_context(_rc()):
        fig, ax = plt.subplots(figsize=(14, 8))
        ax.scatter(x, y, color=th["fact"], s=80, zorder=3, label="Все интервалы")
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
    th = _theme()
    with plt.rc_context(_rc()):
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
            ax.scatter(x, y, color=th["fact"], s=80, zorder=3, label="Отдых")
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


def _fmt_time(sec):
    """Длительность в м:сс (или ч:мм:сс для длинных)."""
    if sec is None:
        return "—"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _plot_table(work, rest, plan_work, plan_rest, title, out_path):
    """Таблица-картинка: строка = повтор, колонки время/темп + отклонение work и rest.
    work/rest — списки (duration_s, pace_sec). Отклонение считается как на графиках:
    work — против linspace(медленный→быстрый край) по номеру; rest — против планового темпа.
    Цвет цифры отклонения — та же схема (_delta_color). Знак +медленнее/−быстрее."""
    n = len(work)
    if n == 0:
        return None
    # эталон work по номеру (как на графике) и единый темп rest
    work_etalon = None
    if plan_work:
        slow, fast = plan_work
        work_etalon = np.linspace(slow, fast, n)

    headers = ["№", "Работа время", "Работа темп", "Работа откл",
               "Отдых время", "Отдых темп", "Отдых откл"]
    rows = []
    # цвета текста по ячейкам отклонений: (row, col) -> color
    cell_colors = {}

    def _dev_str(fact, etalon):
        """Строка отклонения со знаком + цвет. None эталон → («—», None)."""
        if etalon is None or fact is None:
            return "—", None
        d = fact - etalon
        sign = "+" if d > 0 else ("−" if d < 0 else "")
        return f"{sign}{abs(int(round(d)))}", _delta_color(abs(d))

    for i in range(n):
        wt, wp = work[i]
        we = work_etalon[i] if work_etalon is not None else None
        w_dev, w_color = _dev_str(wp, we)
        if i < len(rest):
            rt, rp = rest[i]
            rt_s, rp_s = _fmt_time(rt), _pace_formatter(rp)
            r_dev, r_color = _dev_str(rp, plan_rest)
        else:
            rt_s, rp_s, r_dev, r_color = "—", "—", "—", None
        rows.append([str(i + 1), _fmt_time(wt), _pace_formatter(wp), w_dev,
                     rt_s, rp_s, r_dev])
        if w_color:
            cell_colors[(i + 1, 3)] = w_color  # +1 — строка заголовка сдвигает
        if r_color:
            cell_colors[(i + 1, 6)] = r_color

    fig_h = max(2.5, 0.4 * (n + 2))
    th = _theme()
    with plt.rc_context(_rc()):
        fig, ax = plt.subplots(figsize=(8.5, fig_h))
        ax.axis("off")
        ax.set_title(title, fontsize=13, fontweight="bold", pad=12)
        tbl = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="center")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9)
        tbl.scale(1, 1.3)
        # фон и текст ячеек по теме
        for (r, c), cell in tbl.get_celld().items():
            cell.set_edgecolor(th["box_edge"])
            if r == 0:
                cell.set_facecolor(th["box_face"])
                cell.set_text_props(fontweight="bold", color=th["text"])
            else:
                cell.set_facecolor("none")
                cell.set_text_props(color=th["text"])
        # цвет цифр отклонений (поверх общего цвета текста)
        for (r, col), color in cell_colors.items():
            tbl[r, col].set_text_props(color=color, fontweight="bold")
        plt.tight_layout()
        fig.savefig(out_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
    return out_path


# ══ Сегментная модель (составные повторы: 800 база + 200 ускорение и т.п.) ══
# Эталон work: узкий диапазон pace.zone (|slow-fast|<=EPS) → точное значение
# (горизонталь + коридоры ±5/±10с); широкий → прогрессия linspace по повторам.
WORK_EXACT_EPS = 3.0
WORK_CORR_GREEN = 5
WORK_CORR_YELLOW = 10
_SERIES_COLORS = ["#4da6ff", "#ffa64d", "#7ed957", "#c77dff", "#ff6b6b"]


def _flatten_plan_steps(wkt):
    """[{idx, stype, ttype, dist, bounds(slow,fast)|None}] в порядке исполнения.
    Рекурсия в RepeatGroupDTO; считаются только ExecutableStepDTO (индекс = wktStepIndex)."""
    out = []

    def walk(steps):
        for st in steps or []:
            if not isinstance(st, dict):
                continue
            if st.get("type") == "RepeatGroupDTO" or (st.get("stepType") or {}).get("stepTypeKey") == "repeat":
                walk(st.get("workoutSteps"))
                continue
            stype = (st.get("stepType") or {}).get("stepTypeKey")
            ttype = (st.get("targetType") or {}).get("workoutTargetTypeKey")
            t1 = _ms_to_sec_per_km(st.get("targetValueOne"))
            t2 = _ms_to_sec_per_km(st.get("targetValueTwo"))
            bounds = (max(t1, t2), min(t1, t2)) if (ttype == "pace.zone" and t1 and t2) else None
            out.append({"idx": len(out), "stype": stype, "ttype": ttype,
                        "dist": st.get("endConditionValue"), "bounds": bounds})

    for seg in (wkt.get("workoutSegments") or []):
        walk(seg.get("workoutSteps"))
    return out


def _ordered_laps(splits):
    """Лэпы в хронологии: [{step, dist, dur, pace, intensity}]. Хвост (шаг None) — мимо."""
    laps = (splits.get("lapDTOs") or splits.get("laps") or []) if isinstance(splits, dict) else []
    out = []
    for lp in laps:
        if not isinstance(lp, dict):
            continue
        idx = lp.get("wktStepIndex")
        d = lp.get("distance")
        t = lp.get("duration") or lp.get("movingDuration")
        p = (t / (d / 1000)) if (d and t) else None
        if idx is None or p is None:
            continue
        out.append({"step": idx, "dist": d, "dur": t, "pace": p,
                    "intensity": str(lp.get("intensityType") or "").upper()})
    return out


def _role_of(step, intensity, plan_steps):
    plan = next((s for s in plan_steps if s["idx"] == step), None)
    if plan and plan.get("stype") == "recovery":
        return "rest"
    if plan and plan.get("stype") == "interval":
        return "work"
    return "rest" if intensity in ("RECOVERY", "REST") else "work"


def _step_label(plan_steps, st, fallback_dist):
    plan = next((s for s in plan_steps if s["idx"] == st), None)
    dist = int(plan["dist"]) if (plan and plan.get("dist")) else (int(fallback_dist) if fallback_dist else None)
    return f"{dist} м" if dist else f"шаг {st}"


def _segment_model(ordered, plan_steps):
    """(work_roles[{step,j,label,color,bounds,xs,ys}], x_ticks[(x,'i.j')], rest_paces, S)."""
    work_laps = [l for l in ordered if _role_of(l["step"], l["intensity"], plan_steps) == "work"]
    rest_laps = [l for l in ordered if _role_of(l["step"], l["intensity"], plan_steps) == "rest"]
    work_steps = sorted({l["step"] for l in work_laps})
    j_of = {st: k + 1 for k, st in enumerate(work_steps)}
    S = len(work_steps)
    roles, x_ticks, occ, x = {}, [], {}, 0
    for l in work_laps:
        x += 1
        st = l["step"]
        occ[st] = occ.get(st, 0) + 1
        i, j = occ[st], j_of[st]
        x_ticks.append((x, f"{i}" if S == 1 else f"{i}.{j}"))
        r = roles.setdefault(st, {"step": st, "j": j, "dist": l["dist"], "xs": [], "ys": []})
        r["xs"].append(x)
        r["ys"].append(l["pace"])
    work_roles = []
    for k, st in enumerate(work_steps):
        r = roles[st]
        plan = next((s for s in plan_steps if s["idx"] == st), None)
        r["bounds"] = plan["bounds"] if plan else None
        r["color"] = _SERIES_COLORS[k % len(_SERIES_COLORS)]
        r["label"] = _step_label(plan_steps, st, r["dist"])
        work_roles.append(r)
    return work_roles, x_ticks, [l["pace"] for l in rest_laps], S


def _stats_lines(label, paces):
    arr = np.array(paces)
    imin, imax = int(np.argmin(arr)), int(np.argmax(arr))
    return [label,
            f"  Мин:     {_pace_formatter(arr[imin])}/км (№{imin + 1})",
            f"  Макс:    {_pace_formatter(arr[imax])}/км (№{imax + 1})",
            f"  Среднее: {_pace_formatter(arr.mean())}/км"]


def _plot_work_segmented(work_roles, x_ticks, title, out_path):
    """Интервалы по сегментам: единая хронологическая ось (тики i.j), каждая
    сегмент-роль — точки + эталон + тренд + статблок + отрезки факт↔эталон.
    Эталон: точное значение → горизонталь+коридоры ±5/±10с; диапазон → прогрессия."""
    work_roles = [r for r in work_roles if r["ys"]]
    if not work_roles:
        return None
    th = _theme()
    with plt.rc_context(_rc()):
        fig, ax = plt.subplots(figsize=(15, 8))
        for r in work_roles:
            xs = np.array(r["xs"], dtype=float)
            ys = np.array(r["ys"], dtype=float)
            c = r["color"]
            ax.scatter(xs, ys, color=c, s=75, zorder=3, label=f"{r['label']} — факт")
            if r.get("bounds"):
                slow, fast = r["bounds"]
                if abs(slow - fast) <= WORK_EXACT_EPS:
                    target = (slow + fast) / 2.0
                    ax.axhspan(target - WORK_CORR_YELLOW, target + WORK_CORR_YELLOW,
                               color="gold", alpha=0.15, zorder=1)
                    ax.axhspan(target - WORK_CORR_GREEN, target + WORK_CORR_GREEN,
                               color="green", alpha=0.18, zorder=1)
                    ax.axhline(target, color=c, ls="--", lw=2.4, alpha=0.9, zorder=4,
                               label=f"{r['label']} — эталон {_pace_formatter(target)} (±5/±10с)")
                    et_pts = np.full(len(xs), target)
                else:
                    et_pts = np.linspace(slow, fast, len(xs))
                    ax.plot(xs, et_pts, color=c, ls="--", lw=2.4, alpha=0.85, zorder=4,
                            label=f"{r['label']} — эталон ({_pace_formatter(slow)}→{_pace_formatter(fast)})")
                _draw_deltas(ax, xs, ys, et_pts)
            if len(xs) >= 2:
                a, b = np.polyfit(xs, ys, 1)
                tr = a * xs + b
                ax.plot(xs, tr, color=c, ls=":", lw=2.2, zorder=4,
                        label=f"{r['label']} — тренд ({_pace_formatter(tr[0])}→{_pace_formatter(tr[-1])})")
        for k, r in enumerate(work_roles):
            ax.text(0.02, 0.98 - k * 0.20, "\n".join(_stats_lines(r["label"], r["ys"])),
                    transform=ax.transAxes, fontsize=9, va="top", ha="left",
                    color=r["color"], fontfamily="monospace",
                    bbox=dict(boxstyle="round,pad=0.4", facecolor=th["box_face"],
                              edgecolor=r["color"], alpha=0.9))
        ax.invert_yaxis()
        ax.set_xlabel("Повтор.сегмент", fontsize=12)
        ax.set_ylabel("Темп (мин:сек/км)", fontsize=12)
        ax.set_title(title, fontsize=12, fontweight="bold", pad=58)
        ax.yaxis.set_major_formatter(FuncFormatter(_pace_formatter))
        if x_ticks:
            ax.set_xticks([x for x, _ in x_ticks])
            ax.set_xticklabels([lbl for _, lbl in x_ticks], fontsize=8)
        ax.grid(True, linestyle=":", alpha=0.6)
        handles, labels = ax.get_legend_handles_labels()
        ax.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, 1.0),
                  ncol=min(3, max(1, len(handles))), fontsize=8.5, framealpha=0.9)
        plt.tight_layout()
        fig.savefig(out_path, dpi=110, bbox_inches="tight")
        plt.close(fig)
    return out_path


def _table_model(ordered, plan_steps):
    """(work_steps, role_meta{st:{j,label,bounds,n}}, rows_work{i:{st:(dur,pace)}},
        rows_rest{i:(dur,pace)}, max_i)."""
    work_laps = [l for l in ordered if _role_of(l["step"], l["intensity"], plan_steps) == "work"]
    rest_laps = [l for l in ordered if _role_of(l["step"], l["intensity"], plan_steps) == "rest"]
    work_steps = sorted({l["step"] for l in work_laps})
    j_of = {st: k + 1 for k, st in enumerate(work_steps)}
    role_meta = {}
    for st in work_steps:
        plan = next((s for s in plan_steps if s["idx"] == st), None)
        fb = next((l["dist"] for l in work_laps if l["step"] == st), None)
        role_meta[st] = {"j": j_of[st], "label": _step_label(plan_steps, st, fb),
                         "bounds": plan["bounds"] if plan else None,
                         "n": sum(1 for l in work_laps if l["step"] == st)}
    rows_work, occ = {}, {}
    for l in work_laps:
        st = l["step"]
        occ[st] = occ.get(st, 0) + 1
        rows_work.setdefault(occ[st], {})[st] = (l["dur"], l["pace"])
    rows_rest = {}
    for k, l in enumerate(rest_laps, 1):
        rows_rest[k] = (l["dur"], l["pace"])
    max_i = max(rows_work) if rows_work else 0
    return work_steps, role_meta, rows_work, rows_rest, max_i


def _seg_etalon(meta, i):
    b = meta["bounds"]
    if not b:
        return None
    slow, fast = b
    if abs(slow - fast) <= WORK_EXACT_EPS or meta["n"] <= 1:
        return (slow + fast) / 2.0
    return slow + (fast - slow) * ((i - 1) / (meta["n"] - 1))


def _plot_table_segmented(work_steps, role_meta, rows_work, rows_rest, max_i, has_rest, rest_target, title, out_path):
    if not work_steps or not max_i:
        return None

    def _dev(fact, et):
        if et is None or fact is None:
            return "—", None
        d = fact - et
        sign = "+" if d > 0 else ("−" if d < 0 else "")
        return f"{sign}{abs(int(round(d)))}", _delta_color(abs(d))

    headers = ["№"]
    for st in work_steps:
        lbl = role_meta[st]["label"]
        headers += [f"{lbl}\nвремя", f"{lbl}\nтемп", f"{lbl}\nоткл (сек/км)"]
    if has_rest:
        headers += ["Отдых\nвремя", "Отдых\nтемп", "Отдых\nоткл (сек/км)"]
    rows, cell_colors = [], {}
    for i in range(1, max_i + 1):
        row = [str(i)]
        col = 1
        for st in work_steps:
            cell = rows_work.get(i, {}).get(st)
            if cell:
                dur, pace = cell
                dev, color = _dev(pace, _seg_etalon(role_meta[st], i))
                row += [_fmt_time(dur), _pace_formatter(pace), dev]
                if color:
                    cell_colors[(i, col + 2)] = color
            else:
                row += ["—", "—", "—"]
            col += 3
        if has_rest:
            rc = rows_rest.get(i)
            if rc:
                dev, color = _dev(rc[1], rest_target)
                row += [_fmt_time(rc[0]), _pace_formatter(rc[1]), dev]
                if color:
                    cell_colors[(i, col + 2)] = color
            else:
                row += ["—", "—", "—"]
            col += 3
        rows.append(row)
    th = _theme()
    fig_h = max(2.5, 0.42 * (max_i + 2))
    fig_w = max(6.0, 1.15 * len(headers))
    with plt.rc_context(_rc()):
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))
        ax.axis("off")
        ax.set_title(title, fontsize=12, fontweight="bold", pad=12)
        tbl = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="center")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(8.5)
        tbl.scale(1, 1.5)
        for (rr, cc), cell in tbl.get_celld().items():
            cell.set_edgecolor(th["box_edge"])
            if rr == 0:
                cell.set_facecolor(th["box_face"])
                cell.set_text_props(fontweight="bold", color=th["text"])
            else:
                cell.set_facecolor("none")
                cell.set_text_props(color=th["text"])
        for (rr, cc), color in cell_colors.items():
            tbl[rr, cc].set_text_props(color=color, fontweight="bold")
        plt.tight_layout()
        fig.savefig(out_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
    return out_path


async def build_review(db_user_id: int, out_dir: str = "/tmp", dark: bool = True) -> dict:
    """Главная точка /last: разбор последней DD-активности ПО СЕГМЕНТАМ (составные
    повторы поддержаны). Факт группируется по wktStepIndex, план разворачивается
    пошагово; каждый интервальный сегмент — со своим эталоном/трендом/отклонениями.
    Возвращает {ok, name, work_png, rest_png, table_png, n_work, n_rest, msg}.
    Ключи совместимы с прежней версией. Рабочие ветки бота не трогает."""
    global DARK_MODE
    DARK_MODE = dark

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

    plan_steps = []
    wname = name
    if wkt_id:
        try:
            wkt = await asyncio.to_thread(client.get_workout_by_id, wkt_id)
            plan_steps = _flatten_plan_steps(wkt)
            wname = wkt.get("workoutName") or name
        except Exception as e:
            print(f"build_review: план {wkt_id} недоступен: {type(e).__name__}: {e}")

    ordered = _ordered_laps(splits)
    work_roles, x_ticks, rest_paces, S = _segment_model(ordered, plan_steps)

    base = os.path.join(out_dir, f"s4_{db_user_id}_{act_id}")
    work_png = await asyncio.to_thread(
        _plot_work_segmented, work_roles, x_ticks,
        f"Тренировка: {name}\nЭталон: {wname}", base + "_work.png")

    rest_plan = next((s for s in plan_steps if s["stype"] == "recovery" and s["bounds"]), None)
    rest_target = sum(rest_plan["bounds"]) / 2.0 if rest_plan else None
    rest_png = await asyncio.to_thread(
        _plot_rest, rest_paces, rest_target,
        "Анализ восстановительных интервалов", base + "_rest.png") if rest_paces else None

    ws, rmeta, rw, rr, maxi = _table_model(ordered, plan_steps)
    table_png = await asyncio.to_thread(
        _plot_table_segmented, ws, rmeta, rw, rr, maxi, bool(rest_paces), rest_target,
        "Повторы: время / темп / отклонение", base + "_table.png") if (ws and maxi) else None

    n_work = sum(len(r["ys"]) for r in work_roles)
    return {"ok": True, "name": name, "work_png": work_png, "rest_png": rest_png,
            "table_png": table_png, "n_work": n_work, "n_rest": len(rest_paces),
            "msg": ""}
