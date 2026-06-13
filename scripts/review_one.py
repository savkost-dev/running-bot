"""Прогон разбора (s4_activity) на ПРОИЗВОЛЬНОЙ прошлой DD-тренировке — read-only.

Зачем: команда /last (src/activity_review.build_review) всегда берёт ПОСЛЕДНЮЮ
DD-активность. Этот скрипт позволяет выбрать активность по маске даты (DD_YYYYMMDD)
или по activityId и построить графики разбора — в т.ч. для СОСТАВНЫХ тренировок
(внутри одного повтора куски с разной скоростью, напр. 800 база + 200 ускорение).

МОДЕЛЬ СЕГМЕНТОВ (обобщённая, без хардкода под тип):
Каждый work-лэп получает индекс "повтор.сегмент" (i.j):
  - j (.1/.2/…) — порядковый номер интервального шага внутри повтора, по wktStepIndex;
  - i — какое это по счёту повторение данного шага (по хронологии лэпов);
  - если work-сегмент в повторе ОДИН (простая 25×200) — индекс просто i (без .j).
Роль шага (work/rest) берётся из плана (interval/recovery); если плана нет — из
intensityType (ACTIVE/RECOVERY). Так модель подстраивается под любой тип тренировки.

ЭТАЛОН work (определяется автоматически по диапазону pace.zone из Garmin-workout):
  - узкий диапазон (|slow-fast| <= WORK_EXACT_EPS) → ТОЧНОЕ значение: горизонталь +
    коридоры ±5с (зелёный) и ±10с (жёлтый), по образцу отдыха;
  - широкий диапазон → ПРОГРЕССИЯ: линия linspace(медленный→быстрый) по повторам.

ВЫВОД (PNG в _s4_out/):
  *_work_segmented.png — интервалы: единая хронологическая ось (тики i.j), каждая
    сегмент-роль своим цветом/эталоном/трендом/статблоком, вертикальные отрезки
    факт↔эталон с подписью отклонения и цветом. В заголовке имя тренировки и эталона.
  *_table.png — таблица: строка = повтор, на каждый сегмент 2 столбца (время/темп),
    + отдых; темп подкрашен по отклонению от эталона.
  *_rest.png — восстановительные интервалы.

Переиспользует из src/activity_review.py: _plot_rest, _pace_formatter,
_ms_to_sec_per_km, _theme, _rc, _fmt_time, _delta_color, _draw_deltas.
Сам activity_review.py и bot.py НЕ меняются.

Режимы:
  1) без аргумента → список последних DD-беговых (idx / дата / имя / activityId);
  2) с селектором → разбор + PNG. Селектор: маска (DD_20260609) или activityId.

Пишет в базу: НЕТ (read-only; возможна штатная реавторизация токена Garmin).
Импортирует: garmin, activity_review (оба без bot.py), matplotlib/numpy.
НЕ импортирует bot.py.

Запуск (uid по умолчанию 2 = Anton):
    venv/bin/python3 scripts/review_one.py                    # список
    venv/bin/python3 scripts/review_one.py DD_20260609        # по маске даты
    venv/bin/python3 scripts/review_one.py 23183141466        # по activityId
    venv/bin/python3 scripts/review_one.py DD_20260609 2 light  # светлая тема
"""
import sys
import os
import re
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter

import garmin
import activity_review as ar

_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT_DIR = os.path.join(_PROJECT_ROOT, "_s4_out")

# Палитра серий (читается на тёмном и светлом)
_SERIES_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd", "#8c564b"]

# Эталон work: точное значение (узкий диапазон) → горизонталь + коридоры ±5/±10с;
# широкий диапазон → линия-прогрессия. Порог «точности» в секундах:
WORK_EXACT_EPS = 3.0
WORK_CORR_GREEN = 5
WORK_CORR_YELLOW = 10


def _dd_date(name):
    """YYYYMMDD из маски DD_YYYYMMDD в имени активности; иначе 'unknown'."""
    m = re.search(r"DD_(\d{8})", name or "")
    return m.group(1) if m else "unknown"


def _pace(dist_m, dur_s):
    if not dist_m or not dur_s:
        return "—"
    s = dur_s / (dist_m / 1000)
    return f"{int(s // 60)}:{int(s % 60):02d}"


async def _get_client(uid):
    client = await garmin._client(uid)
    if not client:
        print(f"user={uid}: нет клиента Garmin (токен/подключение).")
    return client


def _dd_runs(acts):
    return [a for a in (acts or [])
            if "running" in str((a.get("activityType") or {}).get("typeKey", ""))
            and "DD_" in str(a.get("activityName") or "")]


async def list_runs(uid):
    client = await _get_client(uid)
    if not client:
        return
    acts = await asyncio.to_thread(client.get_activities, 0, 20)
    runs = _dd_runs(acts)
    if not runs:
        print("Беговой активности с маской DD_ в последних 20 нет.")
        return
    print(f"DD-беговые тренировки (uid={uid}):")
    print(f"  {'#':>2}  {'дата/время':<19}  {'activityId':>14}  имя")
    for i, a in enumerate(runs, 1):
        print(f"  {i:>2}  {str(a.get('startTimeLocal')):<19}  "
              f"{str(a.get('activityId')):>14}  {a.get('activityName')!r}")
    print("\nЗапусти снова с маской (DD_20260609) или activityId.")


def _resolve(acts, selector):
    runs = _dd_runs(acts)
    if selector.isdigit():
        act = next((a for a in (acts or []) if str(a.get("activityId")) == selector), None)
        return act, ([act] if act else [])
    cands = [a for a in runs if selector in str(a.get("activityName") or "")]
    return (cands[0] if cands else None), cands


# ── План: развернуть исполняемые шаги по порядку (индекс = wktStepIndex) ──
def _flatten_plan_steps(wkt):
    """[{idx, stype, ttype, dist, bounds(slow,fast)|None}] в порядке исполнения.
    Рекурсия в RepeatGroupDTO; считаются только ExecutableStepDTO (как wktStepIndex)."""
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
            t1 = ar._ms_to_sec_per_km(st.get("targetValueOne"))
            t2 = ar._ms_to_sec_per_km(st.get("targetValueTwo"))
            bounds = (max(t1, t2), min(t1, t2)) if (ttype == "pace.zone" and t1 and t2) else None
            out.append({"idx": len(out), "stype": stype, "ttype": ttype,
                        "dist": st.get("endConditionValue"), "bounds": bounds})

    for seg in (wkt.get("workoutSegments") or []):
        walk(seg.get("workoutSteps"))
    return out


# ── Факт: лэпы в хронологическом порядке (шаг, дистанция, длительность, темп, тип) ──
def _ordered_laps(splits):
    laps = (splits.get("lapDTOs") or splits.get("laps") or []) if isinstance(splits, dict) else []
    out = []
    for lp in laps:
        if not isinstance(lp, dict):
            continue
        idx = lp.get("wktStepIndex")
        d = lp.get("distance")
        t = lp.get("duration") or lp.get("movingDuration")
        p = (t / (d / 1000)) if (d and t) else None
        if idx is None or p is None:   # хвост-добегание (шаг None) сюда не попадает
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
    """Строит модель сегментов для work-графика.
    Возвращает (work_roles, x_ticks, rest_paces, S):
      work_roles — список {step, j, label, color, bounds, xs[], ys[]} (по сегмент-ролям);
      x_ticks    — [(x, 'i.j')] подписи хронологической оси;
      rest_paces — темпы отдыха в хронологии;
      S          — число work-сегментов в повторе (1 → индексы без .j)."""
    work_laps = [l for l in ordered if _role_of(l["step"], l["intensity"], plan_steps) == "work"]
    rest_laps = [l for l in ordered if _role_of(l["step"], l["intensity"], plan_steps) == "rest"]

    work_steps = sorted({l["step"] for l in work_laps})
    j_of = {st: k + 1 for k, st in enumerate(work_steps)}
    S = len(work_steps)

    roles = {}
    x_ticks = []
    occ = {}
    x = 0
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
            f"  Мин:     {ar._pace_formatter(arr[imin])}/км (№{imin + 1})",
            f"  Макс:    {ar._pace_formatter(arr[imax])}/км (№{imax + 1})",
            f"  Среднее: {ar._pace_formatter(arr.mean())}/км"]


def _plot_work_segmented(work_roles, x_ticks, title, out_path):
    """Единая хронологическая ось; каждая сегмент-роль — точки + эталон + тренд(точки)
    + статблок + отрезки факт↔эталон. Эталон: точное значение → горизонталь + коридоры
    ±5/±10с; диапазон → линия-прогрессия. Тики подписаны i.j."""
    work_roles = [r for r in work_roles if r["ys"]]
    if not work_roles:
        return None
    th = ar._theme()
    with plt.rc_context(ar._rc()):
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
                    ax.axhline(target, color="red", ls="-", lw=3.0, alpha=0.85, zorder=4,
                               label=f"{r['label']} — эталон {ar._pace_formatter(target)} (±5/±10с)")
                    et_pts = np.full(len(xs), target)
                else:
                    et_pts = np.linspace(slow, fast, len(xs))
                    ax.plot(xs, et_pts, color="red", ls="-", lw=3.5, alpha=0.85, zorder=4,
                            label=f"{r['label']} — эталон ({ar._pace_formatter(slow)}→{ar._pace_formatter(fast)})")
                ar._draw_deltas(ax, xs, ys, et_pts)
            if len(xs) >= 2:
                a, b = np.polyfit(xs, ys, 1)
                tr = a * xs + b
                ax.plot(xs, tr, color=c, ls="--", lw=2.4, zorder=4,
                        label=f"{r['label']} — тренд ({ar._pace_formatter(tr[0])}→{ar._pace_formatter(tr[-1])})")
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
        ax.yaxis.set_major_formatter(FuncFormatter(ar._pace_formatter))
        if x_ticks:
            ax.set_xticks([x for x, _ in x_ticks])
            ax.set_xticklabels([lbl for _, lbl in x_ticks], fontsize=8)
        ax.grid(True, linestyle=":", alpha=0.6)
        # Легенда — над полем по центру (вне области данных), авторазбивка на колонки.
        handles, labels = ax.get_legend_handles_labels()
        ax.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, 1.0),
                  ncol=min(3, max(1, len(handles))), fontsize=8.5, framealpha=0.9)
        plt.tight_layout()
        fig.savefig(out_path, dpi=110, bbox_inches="tight")
        plt.close(fig)
    return out_path


# ── Таблица: строка = повтор, на сегмент 2 столбца (время/темп) + отдых ──
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
    """Целевой темп сегмента на повторе i (точное значение либо точка прогрессии)."""
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
        return f"{sign}{abs(int(round(d)))}", ar._delta_color(abs(d))

    headers = ["№"]
    for st in work_steps:
        lbl = role_meta[st]["label"]
        headers += [f"{lbl}\nвремя", f"{lbl}\nтемп", f"{lbl}\nоткл (сек/км)"]
    if has_rest:
        headers += ["Отдых\nвремя", "Отдых\nтемп", "Отдых\nоткл (сек/км)"]

    rows = []
    cell_colors = {}   # (table_row, col) -> цвет ячейки отклонения
    for i in range(1, max_i + 1):
        row = [str(i)]
        col = 1
        for st in work_steps:
            cell = rows_work.get(i, {}).get(st)
            if cell:
                dur, pace = cell
                dev, color = _dev(pace, _seg_etalon(role_meta[st], i))
                row += [ar._fmt_time(dur), ar._pace_formatter(pace), dev]
                if color:
                    cell_colors[(i, col + 2)] = color
            else:
                row += ["—", "—", "—"]
            col += 3
        if has_rest:
            rc = rows_rest.get(i)
            if rc:
                dev, color = _dev(rc[1], rest_target)
                row += [ar._fmt_time(rc[0]), ar._pace_formatter(rc[1]), dev]
                if color:
                    cell_colors[(i, col + 2)] = color
            else:
                row += ["—", "—", "—"]
            col += 3
        rows.append(row)

    th = ar._theme()
    fig_h = max(2.5, 0.42 * (max_i + 2))
    fig_w = max(6.0, 1.15 * len(headers))
    with plt.rc_context(ar._rc()):
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


async def build_for(uid, selector, dark):
    client = await _get_client(uid)
    if not client:
        return
    acts = await asyncio.to_thread(client.get_activities, 0, 20)
    act, cands = _resolve(acts, selector)
    if not act:
        print(f"По селектору {selector!r} ничего не нашёл в последних 20 DD-активностях.")
        return
    if len(cands) > 1:
        print(f"Под маску {selector!r} попало {len(cands)} активностей — беру первую:")
        for a in cands:
            print(f"    {a.get('startTimeLocal')}  id={a.get('activityId')}  {a.get('activityName')!r}")

    act_id, name, wkt_id = act.get("activityId"), act.get("activityName"), act.get("workoutId")
    splits = await asyncio.to_thread(client.get_activity_splits, act_id)

    plan_steps = []
    wname = name
    if wkt_id:
        try:
            wkt = await asyncio.to_thread(client.get_workout_by_id, wkt_id)
            plan_steps = _flatten_plan_steps(wkt)
            wname = wkt.get("workoutName") or name
        except Exception as e:
            print(f"план {wkt_id} недоступен: {type(e).__name__}: {e}")

    print(f"\n=== {name} (activityId={act_id}, workoutId={wkt_id}) ===")
    print(f"Эталонная тренировка: {wname}")
    print(f"План — исполняемые шаги ({len(plan_steps)}):")
    for s in plan_steps:
        b = f"{ar._pace_formatter(s['bounds'][0])} → {ar._pace_formatter(s['bounds'][1])}" if s["bounds"] else "—"
        print(f"  шаг {s['idx']}: {s['stype']:<9} {str(int(s['dist'])) + 'м' if s['dist'] else '?':>6}"
              f"  {s['ttype']:<9}  план {b}")

    ordered = _ordered_laps(splits)
    work_roles, x_ticks, rest_paces, S = _segment_model(ordered, plan_steps)

    print(f"\nМодель сегментов: work-сегментов в повторе S={S}, отдыхов={len(rest_paces)}")
    for r in work_roles:
        b = r.get("bounds")
        if b:
            exact = abs(b[0] - b[1]) <= WORK_EXACT_EPS
            rng = (f"ТОЧНО {ar._pace_formatter((b[0]+b[1])/2)} (±5/±10с)" if exact
                   else f"прогрессия {ar._pace_formatter(b[0])}→{ar._pace_formatter(b[1])}")
        else:
            rng = "нет"
        print(f"  сегмент .{r['j']} ({r['label']}): n={len(r['ys'])}  эталон: {rng}  "
              f"темпы {[ar._pace_formatter(p) for p in r['ys']]}")
    print(f"  подписи оси: {[lbl for _, lbl in x_ticks]}")

    ar.DARK_MODE = dark
    os.makedirs(OUT_DIR, exist_ok=True)
    base = os.path.join(OUT_DIR, f"analysis_{_dd_date(name)}")
    pngs = []

    # 1 — график рабочих интервалов
    if work_roles:
        title = f"Тренировка: {name}\nЭталон: {wname}"
        png = await asyncio.to_thread(_plot_work_segmented, work_roles, x_ticks,
                                      title, base + "_1.png")
        if png:
            pngs.append(png)

    # 2 — график отдыха
    if rest_paces:
        rest_plan = next((s for s in plan_steps if s["stype"] == "recovery" and s["bounds"]), None)
        target = sum(rest_plan["bounds"]) / 2.0 if rest_plan else None
        png = await asyncio.to_thread(ar._plot_rest, rest_paces, target,
                                      "Анализ восстановительных интервалов", base + "_2.png")
        if png:
            pngs.append(png)

    # 3 — таблица
    ws, rmeta, rw, rr, maxi = _table_model(ordered, plan_steps)
    if ws and maxi:
        rest_plan = next((s for s in plan_steps if s["stype"] == "recovery" and s["bounds"]), None)
        rest_target = sum(rest_plan["bounds"]) / 2.0 if rest_plan else None
        png = await asyncio.to_thread(_plot_table_segmented, ws, rmeta, rw, rr, maxi,
                                      bool(rest_paces), rest_target,
                                      "Повторы: время / темп / отклонение", base + "_3.png")
        if png:
            pngs.append(png)

    print("\nГотово, PNG:")
    for p in pngs:
        print(f"  {p}")


async def main():
    args = list(sys.argv[1:])
    dark = True
    if args and args[-1].lower() in ("light", "dark"):
        dark = args.pop().lower() == "dark"
    if not args:
        await list_runs(2)
        return
    selector = args[0]
    uid = int(args[1]) if len(args) > 1 else 2
    await build_for(uid, selector, dark)


if __name__ == "__main__":
    asyncio.run(main())
