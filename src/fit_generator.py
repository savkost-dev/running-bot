"""Garmin Workout API JSON generator for Dusty Dumbbells running bot."""
import re
from datetime import datetime


# ── Pace / speed helpers ──────────────────────────────────────

def _pace_to_ms(pace: str) -> float:
    """'M:SS' min/km → m/s. Returns 0.0 on error."""
    m = re.match(r'(\d+):(\d{2})', pace.strip())
    if not m:
        return 0.0
    sec = int(m.group(1)) * 60 + int(m.group(2))
    return round(1000.0 / sec, 7) if sec > 0 else 0.0


_pace_to_speed = _pace_to_ms  # backward compat alias


def _pace_range_speeds(pace_range: str) -> tuple[float, float]:
    """'4:00–4:25 мин/км' → (slow_mps, fast_mps). Returns (0, 0) on failure."""
    paces = re.findall(r'(\d+:\d{2})', pace_range)
    speeds = sorted([_pace_to_ms(p) for p in paces if _pace_to_ms(p) > 0])
    if len(speeds) >= 2:
        return speeds[0], speeds[-1]
    if speeds:
        return speeds[0] * 0.95, speeds[0] * 1.05
    return 0.0, 0.0


# ── Group text parsers ────────────────────────────────────────

def _group_block(groups_raw: str, group_num: str) -> str:
    """Extract lines belonging to group_num from groups_raw."""
    EMOJIS = {
        '1️⃣': '1', '2️⃣': '2', '3️⃣': '3', '4️⃣': '4',
        '5️⃣': '5', '6️⃣': '6', '7️⃣': '7', '8️⃣': '8',
    }
    target = str(group_num).split('.')[0]
    result, active = [], False
    for line in groups_raw.split('\n'):
        s = line.strip()
        num = None
        for e, n in EMOJIS.items():
            if e in s:
                num = n
                break
        if num is None:
            m = re.search(r'[Гг]руппа\s*(\d+)', s)
            if m:
                num = m.group(1)
        if num is not None:
            if num == target and not active:
                active = True
                result.append(s)
            elif active:
                break
        elif active and s:
            result.append(s)
    return '\n'.join(result)


def _interval_speeds(block: str, dist_m: float) -> tuple[float, float]:
    """Extract (slow_mps, fast_mps) for interval from group block.

    Handles three formats:
    1. '137–130 сек'           — explicit seconds
    2. '2:17–2:10'             — M:SS as time for the interval (not pace/km)
    3. '4:05–3:40' / '4.05–3.40' — pace per km (colon or dot notation)
    """
    # 1. Explicit seconds: "137–130 сек"
    m = re.search(r'(\d{2,3})\s*[-–]\s*(\d{2,3})\s*сек', block)
    if m:
        t1, t2 = float(m.group(1)), float(m.group(2))
        t_slow, t_fast = max(t1, t2), min(t1, t2)
        return dist_m / t_slow, dist_m / t_fast

    # 2 & 3. M:SS format — determine if it's pace/km or time for the interval
    paces = re.findall(r'\b(\d+:\d{2})\b', block)
    if paces:
        raw_speeds = [_pace_to_ms(p) for p in paces if _pace_to_ms(p) > 0]
        if raw_speeds:
            # Running pace faster than 5.5 m/s (~3:02/km) is implausible →
            # values are interval TIMES (e.g. "2:17 for 500 m"), not pace/km
            if max(raw_speeds) > 5.5 and dist_m > 0:
                time_speeds = []
                for p in paces:
                    parts = p.split(':')
                    sec = int(parts[0]) * 60 + int(parts[1])
                    if sec > 0:
                        time_speeds.append(round(dist_m / sec, 7))
                if time_speeds:
                    time_speeds.sort()
                    return time_speeds[0], time_speeds[-1]
            else:
                raw_speeds.sort()
                return raw_speeds[0], raw_speeds[-1]

    # 4. Dot notation pace: "4.05–3.40" (M.SS min/km, M in range 3–7)
    dot_paces = re.findall(r'\b([3-7]\.\d{2})\b', block)
    if dot_paces:
        speeds = []
        for p in dot_paces:
            mins_s, secs_s = p.split('.')
            sec = int(mins_s) * 60 + int(secs_s)
            if sec > 0:
                speeds.append(round(1000.0 / sec, 7))
        if speeds:
            speeds.sort()
            return speeds[0], speeds[-1]

    return 0.0, 0.0


def _tempo_speeds(block: str) -> tuple[float, float]:
    """Extract (slow_mps, fast_mps) for tempo piece from group block."""
    m = re.search(r'\d+\s*км\s*:\s*([\d:/\s]+)', block)
    if m:
        paces = re.findall(r'(\d:\d{2})', m.group(1))
        speeds = sorted([_pace_to_ms(p) for p in paces if _pace_to_ms(p) > 0])
        if speeds:
            return speeds[0], speeds[-1]
    return _interval_speeds(block, 1000.0)


def _rest_speed(block: str, rest_m: float) -> float:
    """Parse recovery speed from patterns like '300м — 1:30' in group block.

    Examples:
      '10x500/300м — 1:30'  → 300 / 90 = 3.333 m/s  (tempo 5:00 /km)
      '6x1000/400м — 2:30'  → 400 / 150 = 2.667 m/s  (tempo 6:15 /km)
      '300м лёгкий бег'     → 0.0  (no time → no.target)
    Returns m/s or 0.0 if no timed recovery found.
    """
    rest_int = int(rest_m)
    # Match: {rest_m}м — M:SS  (accepts —, –, -)
    for pat in [
        rf'\b{rest_int}\s*м\s*[—–\-]\s*(\d+):(\d{{2}})',
        rf'[/,]\s*{rest_int}\s*м\s*[—–\-]\s*(\d+):(\d{{2}})',
    ]:
        m = re.search(pat, block, re.IGNORECASE)
        if m:
            total_sec = int(m.group(1)) * 60 + int(m.group(2))
            if total_sec > 0:
                return round(rest_m / total_sec, 7)
    return 0.0


def _tempo_pace_list(block: str) -> list[float]:
    """Parse 'N км: p1/p2/p3/p4' → list of m/s values (one per km)."""
    m = re.search(r'\d+\s*км\s*:\s*([\d:/ ]+)', block)
    if not m:
        return []
    paces = re.findall(r'(\d+:\d{2})', m.group(1))
    return [_pace_to_ms(p) for p in paces if _pace_to_ms(p) > 0]


def _work_struct(work_text: str) -> dict:
    """Parse work_text → {reps, work_m, rest_m, tempo_km?}."""
    r = {}
    for pat in [
        r'(\d+)\s*(?:по|×|x)\s*(\d+)\s*/\s*(\d+)\s*(?:м\b)',
        r'(\d+)\s*[×xхX]\s*(\d+)\s*/\s*(\d+)',
    ]:
        m = re.search(pat, work_text, re.IGNORECASE)
        if m:
            r['reps'] = int(m.group(1))
            r['work_m'] = float(m.group(2))
            r['rest_m'] = float(m.group(3))
            break
    if r.get('reps'):
        km = re.search(r'(\d+(?:[.,]\d+)?)\s*км', work_text)
        if km:
            r['tempo_km'] = float(km.group(1).replace(',', '.'))
    return r


# ── Garmin JSON step builders ─────────────────────────────────

_SPORT = {'sportTypeId': 1, 'sportTypeKey': 'running', 'displayOrder': 1}
_STROKE = {'strokeTypeId': 0, 'strokeTypeKey': None, 'displayOrder': 0}
_EQUIP = {'equipmentTypeId': 0, 'equipmentTypeKey': None, 'displayOrder': 0}
_STYPE = {'interval': 3, 'recovery': 4, 'warmup': 1, 'cooldown': 2}


def _step(order: int, dist_m: float | None = None, time_s: float | None = None,
          v_fast: float | None = None, v_slow: float | None = None,
          stype: str = 'interval', child_id: int | None = None) -> dict:
    """Build an ExecutableStepDTO matching the Garmin Connect API format.

    targetValueOne = fast speed (higher m/s), targetValueTwo = slow speed (lower m/s).
    For a single-pace target pass v_fast == v_slow.
    """
    is_pace = v_fast is not None and v_fast > 0
    by_dist = dist_m is not None
    sid = _STYPE.get(stype, 3)
    return {
        'type': 'ExecutableStepDTO',
        'stepOrder': order,
        'stepType': {'stepTypeId': sid, 'stepTypeKey': stype, 'displayOrder': sid},
        'childStepId': child_id,
        'description': None,
        'endCondition': {
            'conditionTypeId': 3 if by_dist else 2,
            'conditionTypeKey': 'distance' if by_dist else 'time',
            'displayOrder': 3 if by_dist else 2,
            'displayable': True,
        },
        'endConditionValue': float(dist_m if by_dist else time_s),
        'preferredEndConditionUnit': None,
        'endConditionCompare': None,
        'targetType': {
            'workoutTargetTypeId': 6 if is_pace else 1,
            'workoutTargetTypeKey': 'pace.zone' if is_pace else 'no.target',
            'displayOrder': 6 if is_pace else 1,
        },
        'targetValueOne': round(v_fast, 7) if is_pace else None,
        'targetValueTwo': round(v_slow, 7) if is_pace else None,
        'targetValueUnit': None,
        'zoneNumber': None,
        'secondaryTargetType': None,
        'secondaryTargetValueOne': None,
        'secondaryTargetValueTwo': None,
        'secondaryTargetValueUnit': None,
        'secondaryZoneNumber': None,
        'endConditionZone': None,
        'strokeType': _STROKE,
        'equipmentType': _EQUIP,
        'category': None,
        'exerciseName': None,
        'workoutProvider': None,
        'providerExerciseSourceId': None,
        'weightValue': None,
        'weightUnit': None,
    }


def _repeat_group(order: int, iterations: int, inner_steps: list) -> dict:
    """Build a RepeatGroupDTO."""
    return {
        'type': 'RepeatGroupDTO',
        'stepOrder': order,
        'stepType': {'stepTypeId': 6, 'stepTypeKey': 'repeat', 'displayOrder': 6},
        'childStepId': 1,
        'numberOfIterations': iterations,
        'workoutSteps': inner_steps,
        'endCondition': {
            'conditionTypeId': 7,
            'conditionTypeKey': 'iterations',
            'displayOrder': 7,
            'displayable': False,
        },
        'endConditionValue': float(iterations),
        'preferredEndConditionUnit': None,
        'endConditionCompare': None,
        'skipLastRestStep': False,
        'smartRepeat': False,
    }


def _workout_json(name: str, steps: list) -> dict:
    """Wrap steps into a Garmin workout JSON."""
    return {
        'workoutName': name,
        'description': None,
        'sportType': _SPORT,
        'subSportType': None,
        'workoutSegments': [{
            'segmentOrder': 1,
            'sportType': _SPORT,
            'poolLengthUnit': None,
            'poolLength': None,
            'workoutSteps': steps,
        }],
    }


# ── Filename helpers ──────────────────────────────────────────

def workout_filename(workout_date: str, group_num: str) -> str:
    """'2026-05-22', '3.5' → 'DD_20260522-3.5_lvl.json'"""
    d = workout_date.replace('-', '')
    return f'DD_{d}-{group_num}_lvl.json'


def interval_filename(workout_date: str, group_num: str) -> str:
    return workout_filename(workout_date, group_num)


def long_run_filename(workout_date: str, group_num: str) -> str:
    return workout_filename(workout_date, group_num)


# ── Core builders ─────────────────────────────────────────────

def _build_interval_json(workout: dict, group: str, recommended_pace: str = '') -> dict:
    date = workout.get('workout_date', datetime.now().strftime('%Y-%m-%d'))
    work_text = workout.get('work_text', '')
    groups_raw = workout.get('groups_raw', '')
    for raw in (workout.get('extra_groups_raw') or []):
        groups_raw += '\n' + raw

    block = _group_block(groups_raw, group)
    st = _work_struct(work_text)
    reps = st.get('reps', 10)
    work_m = st.get('work_m', 200.0)
    rest_m = st.get('rest_m', work_m)
    tempo_km = st.get('tempo_km')

    int_slow, int_fast = _interval_speeds(block, work_m)
    if (int_slow == 0.0 or int_fast == 0.0) and recommended_pace:
        paces = re.findall(r'(\d+:\d{2})', recommended_pace)
        speeds = sorted([_pace_to_ms(p) for p in paces if _pace_to_ms(p) > 0])
        if len(speeds) >= 2:
            int_slow, int_fast = speeds[0], speeds[-1]
        elif speeds:
            int_slow, int_fast = speeds[0] * 0.95, speeds[0] * 1.05

    order = 1
    wkt_steps = []

    if tempo_km:
        n_km = int(tempo_km)
        pace_list = _tempo_pace_list(block)
        if pace_list:
            # Individual 1000m steps with per-km paces (matches Garmin example format)
            for v in pace_list[:n_km]:
                wkt_steps.append(_step(order, dist_m=1000.0, v_fast=v, v_slow=v))
                order += 1
        else:
            # Derive average pace from recommended_pace for groups without per-km breakdown
            t_speed = 0.0
            if recommended_pace:
                paces = re.findall(r'(\d+:\d{2})', recommended_pace)
                speeds = [_pace_to_ms(p) for p in paces if _pace_to_ms(p) > 0]
                if speeds:
                    t_speed = sum(speeds) / len(speeds)
            for _ in range(n_km):
                wkt_steps.append(_step(order, dist_m=1000.0,
                    v_fast=t_speed if t_speed > 0 else None,
                    v_slow=t_speed if t_speed > 0 else None))
                order += 1
        # 400m recovery between tempo and repeats
        wkt_steps.append(_step(order, dist_m=400.0, stype='recovery'))
        order += 1

    # RepeatGroupDTO — step order before inner steps
    repeat_order = order
    inner_order = order + 1
    rest_speed = _rest_speed(block, rest_m)
    inner = [
        _step(inner_order, dist_m=work_m, v_fast=int_fast, v_slow=int_slow, child_id=1),
        _step(inner_order + 1, dist_m=rest_m, stype='recovery',
              v_fast=rest_speed if rest_speed > 0 else None,
              v_slow=rest_speed if rest_speed > 0 else None,
              child_id=1),
    ]
    wkt_steps.append(_repeat_group(repeat_order, reps, inner))

    d = date.replace('-', '')
    return _workout_json(f'DD_{d}-{group}_lvl', wkt_steps)


def _build_long_run_json(workout: dict, group: str, strategy: str,
                          first_half_pace: str, second_half_pace: str | None) -> dict:
    date = workout.get('workout_date', datetime.now().strftime('%Y-%m-%d'))
    v1 = _pace_to_ms(first_half_pace)
    steps = []
    if strategy == 'progressive' and second_half_pace:
        v2 = _pace_to_ms(second_half_pace)
        steps.append(_step(1, time_s=50 * 60, v_fast=v1, v_slow=v1))
        steps.append(_step(2, time_s=50 * 60, v_fast=v2, v_slow=v2))
    else:
        steps.append(_step(1, time_s=100 * 60, v_fast=v1, v_slow=v1))

    d = date.replace('-', '')
    return _workout_json(f'DD_{d}-{group}_lvl', steps)


# ── Public API ────────────────────────────────────────────────

def create_garmin_workout(workout: dict, recommended_group: str,
                          recommended_pace: str = '') -> dict:
    """Return Garmin Connect workout JSON ready for upload.

    Interval workout: workout must have 'work_text' and 'groups_raw'.
    Long run: workout must have 'groups' list and optionally
    'strategy', 'first_half_pace', 'second_half_pace'.
    """
    if 'work_text' in workout:
        return _build_interval_json(workout, recommended_group, recommended_pace)
    strategy = workout.get('strategy', 'even')
    first_half = workout.get('first_half_pace', '')
    second_half = workout.get('second_half_pace')
    return _build_long_run_json(workout, recommended_group, strategy, first_half, second_half)


async def upload_to_garmin(workout_json: dict, db_user_id: int) -> bool:
    """Upload workout JSON to Garmin Connect. Returns True on success."""
    from garmin import upload_workout
    return await upload_workout(db_user_id, workout_json)


# ── Backward compatibility aliases ───────────────────────────

def build_garmin_interval_workout(workout: dict, recommended_group: str,
                                   recommended_pace: str = '') -> dict:
    return _build_interval_json(workout, recommended_group, recommended_pace)


def build_garmin_long_run_workout(workout: dict, recommended_group: str,
                                   strategy: str, first_half_pace: str,
                                   second_half_pace: str | None) -> dict:
    return _build_long_run_json(workout, recommended_group, strategy,
                                 first_half_pace, second_half_pace)
