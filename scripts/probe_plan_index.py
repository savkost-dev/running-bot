"""Диагностика нумерации wktStepIndex: сырой план Garmin vs фактические лэпы.

READ-ONLY. В базу НЕ пишет, боевые модули НЕ меняет (только импортирует
activity_review и garmin для доступа к данным). Задача — подтвердить/опровергнуть
гипотезу о том, как Garmin нумерует шаги при нескольких repeat-группах:
repeat-группа занимает СВОЙ индекс и встаёт ПОСЛЕ своих детей.

Печатает две таблицы:
  (а) шаги плана из сырого workout JSON в порядке FIT-перечисления (дети, затем
      группа), с типом (Executable/RepeatGroup), stepId/stepOrder, дистанцией,
      целевыми темпами, СТАРЫМ idx (_flatten_plan_steps) и ГИПОТЕЗОЙ нового idx;
  (б) лэпы факта: wktStepIndex, intensityType, дистанция, длительность, темп.
Плюс вердикт: под какой схемой (старой/новой) роли лэпов сходятся с планом.

Запуск (на сервере):
    venv/bin/python3 scripts/probe_plan_index.py            # uid=2, последняя DD
    venv/bin/python3 scripts/probe_plan_index.py 2 DD_20260724
    venv/bin/python3 scripts/probe_plan_index.py 2 23710783350
"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import garmin
import activity_review as ar
import ai_package


# ── формат ──────────────────────────────────────────────────────────────
def _pace(v):
    """сек/км → 'м:сс' (v уже в сек/км) либо '—'."""
    if not v or v <= 0:
        return "—"
    return f"{int(v // 60)}:{int(v % 60):02d}"


def _pace_from_ms(ms):
    """m/s → сек/км строкой."""
    s = ar._ms_to_sec_per_km(ms)
    return _pace(s)


def _is_repeat(st):
    return st.get("type") == "RepeatGroupDTO" or \
        (st.get("stepType") or {}).get("stepTypeKey") == "repeat"


# ── обход сырого плана с ДВУМЯ схемами нумерации ─────────────────────────
def _walk_raw(wkt):
    """Возвращает список строк в порядке FIT-перечисления (дети, затем группа).
    Каждая строка = dict со СТАРЫМ idx (как в _flatten_plan_steps: индекс только
    у executable-шагов, группа его НЕ занимает) и НОВЫМ idx (гипотеза: сквозной
    счётчик, где repeat-группа занимает свой номер ПОСЛЕ детей)."""
    new_c = [0]
    old_c = [0]
    rows = []

    def walk(steps, depth):
        for st in steps or []:
            if not isinstance(st, dict):
                continue
            if _is_repeat(st):
                walk(st.get("workoutSteps"), depth + 1)
                ni = new_c[0]; new_c[0] += 1
                rows.append({
                    "kind": "RepeatGroup",
                    "new_idx": ni, "old_idx": None, "depth": depth,
                    "stepId": st.get("stepId"), "stepOrder": st.get("stepOrder"),
                    "stype": "repeat",
                    "iters": st.get("numberOfIterations"),
                    "dist": None, "t1": None, "t2": None,
                })
            else:
                ni = new_c[0]; new_c[0] += 1
                oi = old_c[0]; old_c[0] += 1
                rows.append({
                    "kind": "Executable",
                    "new_idx": ni, "old_idx": oi, "depth": depth,
                    "stepId": st.get("stepId"), "stepOrder": st.get("stepOrder"),
                    "stype": (st.get("stepType") or {}).get("stepTypeKey"),
                    "iters": None,
                    "dist": st.get("endConditionValue"),
                    "t1": st.get("targetValueOne"), "t2": st.get("targetValueTwo"),
                })

    for seg in (wkt.get("workoutSegments") or []):
        walk(seg.get("workoutSteps"), 0)
    return rows


def _role_from_stype(stype):
    """Ожидаемая интенсивность лэпа по типу шага плана."""
    if stype == "interval":
        return "ACTIVE"
    if stype in ("recovery", "rest"):
        return "RECOVERY"
    if stype in ("warmup",):
        return "WARMUP"
    if stype in ("cooldown",):
        return "COOLDOWN"
    return "?"


# ── печать ──────────────────────────────────────────────────────────────
def _print_plan(rows, flat):
    print("\n=== (а) ПЛАН: шаги из сырого workout JSON ===")
    print("Порядок строк = FIT-перечисление (дети, затем repeat-группа).")
    print(f"{'new':>3} {'old':>3}  {'тип':<12} {'stepType':<9} {'stepOrd':>7} "
          f"{'stepId':>8} {'dist':>7} {'target(медл→быстр)':<20} iters")
    print("-" * 92)
    for r in rows:
        pad = "  " * r["depth"]
        old = "—" if r["old_idx"] is None else str(r["old_idx"])
        dist = f"{int(r['dist'])}" if r["dist"] else "—"
        if r["t1"] and r["t2"]:
            p1, p2 = _pace_from_ms(r["t1"]), _pace_from_ms(r["t2"])
            tgt = f"{p1} → {p2}"
        else:
            tgt = "—"
        iters = str(r["iters"]) if r["iters"] else ""
        print(f"{r['new_idx']:>3} {old:>3}  {pad}{r['kind']:<12} "
              f"{str(r['stype'] or ''):<9} {str(r['stepOrder'] or ''):>7} "
              f"{str(r['stepId'] or ''):>8} {dist:>7} {tgt:<20} {iters}")

    # сверка: наш скрипт old_idx == реальный _flatten_plan_steps
    script_old = [r["old_idx"] for r in rows if r["old_idx"] is not None]
    real_old = [s["idx"] for s in flat]
    ok = script_old == real_old
    print(f"\nСверка старой схемы со _flatten_plan_steps: "
          f"{'OK' if ok else 'РАСХОЖДЕНИЕ'} "
          f"(скрипт {script_old} vs модуль {real_old})")


def _print_laps(ordered):
    print("\n=== (б) ФАКТ: лэпы (в хронологии) ===")
    print(f"{'#':>3} {'wktStepIdx':>10} {'intensity':<10} {'dist':>7} "
          f"{'dur':>7} {'pace':>7}")
    print("-" * 52)
    for n, l in enumerate(ordered, 1):
        dur = l["dur"]
        durs = f"{int(dur // 60)}:{int(dur % 60):02d}" if dur else "—"
        print(f"{n:>3} {str(l['step']):>10} {l['intensity']:<10} "
              f"{int(l['dist']):>7} {durs:>7} {_pace(l['pace']):>7}")


def _verdict(rows, ordered):
    """Для каждой схемы (old/new) считаем, у скольких work-лэпов найденный шаг
    плана имеет совместимую роль (interval для ACTIVE, recovery для RECOVERY)."""
    by_old = {r["old_idx"]: r for r in rows if r["old_idx"] is not None}
    by_new = {r["new_idx"]: r for r in rows}

    def score(table):
        hit = miss = orphan = 0
        for l in ordered:
            r = table.get(l["step"])
            if r is None:
                orphan += 1
                continue
            want = _role_from_stype(r["stype"])
            got = l["intensity"]
            # ACTIVE↔interval, RECOVERY/REST↔recovery — грубая совместимость
            compat = (want == got) or \
                (want == "ACTIVE" and got == "ACTIVE") or \
                (want == "RECOVERY" and got in ("RECOVERY", "REST"))
            if compat:
                hit += 1
            else:
                miss += 1
        return hit, miss, orphan

    print("\n=== ВЕРДИКТ: совместимость роли лэпа с найденным шагом плана ===")
    for name, table in (("СТАРАЯ (группа НЕ занимает idx)", by_old),
                        ("НОВАЯ  (группа занимает idx после детей)", by_new)):
        hit, miss, orphan = score(table)
        print(f"  {name}: совпало {hit}, несовпало {miss}, "
              f"нет шага плана {orphan}")
    print("  Ожидание при верной гипотезе: у НОВОЙ схемы miss=0, у СТАРОЙ miss>0.")


# ── main ────────────────────────────────────────────────────────────────
async def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    selector = sys.argv[2] if len(sys.argv) > 2 else None

    client = await garmin._client(uid)
    if not client:
        print(f"Garmin не подключён для uid={uid}.")
        return
    acts = await asyncio.to_thread(client.get_activities, 0, 20)
    act = ai_package._pick_activity(acts, selector)
    if not act:
        print("DD-активность не найдена (проверь selector).")
        return

    act_id = act.get("activityId")
    name = act.get("activityName")
    wkt_id = act.get("workoutId")
    print(f"Активность: {name}  (activityId={act_id}, workoutId={wkt_id})")

    if not wkt_id:
        print("У активности нет workoutId — план из Garmin недоступен.")
        return

    wkt = await asyncio.to_thread(client.get_workout_by_id, wkt_id)
    splits = await asyncio.to_thread(client.get_activity_splits, act_id)

    rows = _walk_raw(wkt)
    flat = ar._flatten_plan_steps(wkt)
    ordered = ar._ordered_laps(splits)

    _print_plan(rows, flat)
    _print_laps(ordered)
    _verdict(rows, ordered)


if __name__ == "__main__":
    asyncio.run(main())
