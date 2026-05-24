"""Test Garmin workout JSON generation."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from fit_generator import (
    create_garmin_workout, workout_filename,
    build_garmin_interval_workout, build_garmin_long_run_workout,
    interval_filename, long_run_filename,
    _group_block, _work_struct, _interval_speeds, _tempo_speeds, _tempo_pace_list,
)

# ── Mock data ─────────────────────────────────────────────────

INTERVAL_WORKOUT = {
    'workout_date': '2026-05-27',
    'work_text': '4 км + 10 по 200/200 м',
    'groups_raw': (
        '1️⃣ Группа\n4 км: 3:20/3:35/3:35/3:10\n400 м – лёгкий бег\n200 м – 38-33 сек\n\n'
        '2️⃣ Группа\n4 км: 3:35/3:50/3:50/3:25\n400 м – лёгкий бег\n200 м – 42-35 сек\n\n'
        '3️⃣ Группа\n4 км: 3:45/4:00/4:00/3:35\n400 м – лёгкий бег\n200 м – 45-38 сек\n\n'
        '4️⃣ Группа\n4 км: 4:25/4:50/4:50/4:15\n400 м – лёгкий бег\n200 м – 52-48 сек'
    ),
    'extra_groups_raw': [
        '3.5 группа\nРабота: 4 км\n200м - 48-40 сек',
    ],
}

LONG_WORKOUT = {
    'workout_date': '2026-05-25',
    'groups': [
        {'number': '4', 'pace_start': '5:30', 'pace_end': '5:00', 'progression': True},
        {'number': '5', 'pace_start': '6:00', 'pace_end': '5:30', 'progression': True},
    ],
}

SIMPLE_INTERVALS = {
    'workout_date': '2026-06-03',
    'work_text': '8×400/400м',
    'groups_raw': '3️⃣ Группа\n400 м – 1:42-1:35\n400 м – лёгкий бег',
    'extra_groups_raw': [],
}


def test_parse_group_block():
    block = _group_block(INTERVAL_WORKOUT['groups_raw'], '3')
    assert '3️⃣' in block or 'Группа' in block, f'Block not found: {block!r}'
    assert '200 м – 45-38 сек' in block, f'Time not in block: {block!r}'
    print(f'✅ Group block parsed: {len(block)} chars')


def test_parse_work_struct():
    st = _work_struct('4 км + 10 по 200/200 м')
    assert st['reps'] == 10
    assert st['work_m'] == 200.0
    assert st['rest_m'] == 200.0
    assert st.get('tempo_km') == 4.0

    st2 = _work_struct('8×400/400м')
    assert st2['reps'] == 8
    assert st2['work_m'] == 400.0

    st3 = _work_struct('6 по 1000/400 м')
    assert st3['reps'] == 6
    assert st3['work_m'] == 1000.0
    assert st3['rest_m'] == 400.0
    print('✅ Work structure parsing OK')


def test_interval_speeds():
    block = _group_block(INTERVAL_WORKOUT['groups_raw'], '3')
    sl, sh = _interval_speeds(block, 200.0)
    assert sl > 0, f'slow speed=0: block={block!r}'
    assert sh > sl, f'fast not faster than slow: sl={sl}, sh={sh}'
    # 200m in 45-38 sec → slow=200/45≈4.44m/s, fast=200/38≈5.26m/s
    assert abs(sl - 200/45) < 0.1, f'Expected ~{200/45:.2f} got {sl:.2f}'
    assert abs(sh - 200/38) < 0.1, f'Expected ~{200/38:.2f} got {sh:.2f}'
    print(f'✅ Interval speeds: slow={sl:.2f}m/s ({1000/sl:.0f}s/km), fast={sh:.2f}m/s ({1000/sh:.0f}s/km)')


def test_tempo_pace_list():
    block = _group_block(INTERVAL_WORKOUT['groups_raw'], '3')
    paces = _tempo_pace_list(block)
    assert len(paces) == 4, f'Expected 4 paces, got {len(paces)}: {paces}'
    # 3:45/4:00/4:00/3:35 → speeds in m/s
    assert abs(paces[0] - 1000/225) < 0.01, f'Pace 0: {paces[0]:.4f}'  # 3:45
    assert abs(paces[1] - 1000/240) < 0.01, f'Pace 1: {paces[1]:.4f}'  # 4:00
    assert abs(paces[3] - 1000/215) < 0.01, f'Pace 3: {paces[3]:.4f}'  # 3:35
    print(f'✅ Tempo pace list: {[f"{1000/v:.0f}s/km" for v in paces]}')


def test_create_interval_json():
    j = create_garmin_workout(INTERVAL_WORKOUT, '3', '3:45–4:00 мин/км')
    assert j['workoutName'] == 'DD_20260527-3_lvl', j['workoutName']
    steps = j['workoutSegments'][0]['workoutSteps']
    # 4 tempo + 1 recovery + 1 RepeatGroup = 6 outer steps
    assert len(steps) == 6, f'Expected 6 outer steps, got {len(steps)}: {[s["type"] for s in steps]}'
    repeat = steps[-1]
    assert repeat['type'] == 'RepeatGroupDTO'
    assert repeat['numberOfIterations'] == 10
    assert len(repeat['workoutSteps']) == 2
    # Tempo steps have targetValueOne == targetValueTwo (single pace)
    for i in range(4):
        s = steps[i]
        assert s['endConditionValue'] == 1000.0
        assert s['targetValueOne'] == s['targetValueTwo']
        assert s['targetValueOne'] is not None
    fname = workout_filename('2026-05-27', '3')
    assert fname == 'DD_20260527-3_lvl.json', fname
    print(f'✅ Interval JSON: {j["workoutName"]}, 6 outer steps → {fname}')


def test_create_interval_json_grp35():
    j = create_garmin_workout(INTERVAL_WORKOUT, '3.5', '4:00–4:25 мин/км')
    assert j['workoutName'] == 'DD_20260527-3.5_lvl', j['workoutName']
    steps = j['workoutSegments'][0]['workoutSteps']
    # No per-km paces in extra group → 4 steps at avg pace + recovery + repeat
    assert len(steps) == 6, f'Expected 6 steps, got {len(steps)}'
    fname = workout_filename('2026-05-27', '3.5')
    assert fname == 'DD_20260527-3.5_lvl.json', fname
    print(f'✅ Interval JSON grp3.5: {j["workoutName"]} → {fname}')


def test_create_interval_json_no_tempo():
    j = create_garmin_workout(SIMPLE_INTERVALS, '3', '3:30–3:45 мин/км')
    steps = j['workoutSegments'][0]['workoutSteps']
    # No tempo → only RepeatGroupDTO
    assert len(steps) == 1, f'Expected 1 step, got {len(steps)}'
    assert steps[0]['type'] == 'RepeatGroupDTO'
    assert steps[0]['numberOfIterations'] == 8
    print(f'✅ Interval JSON (no tempo): 1 outer step (RepeatGroup×8)')


def test_create_long_run_progressive():
    wkt = {**LONG_WORKOUT, 'strategy': 'progressive',
           'first_half_pace': '5:30', 'second_half_pace': '5:00'}
    j = create_garmin_workout(wkt, '4')
    steps = j['workoutSegments'][0]['workoutSteps']
    assert len(steps) == 2, f'Expected 2 steps, got {len(steps)}'
    assert steps[0]['endConditionValue'] == 3000.0   # 50 min
    assert steps[1]['endConditionValue'] == 3000.0
    fname = workout_filename('2026-05-25', '4')
    assert fname == 'DD_20260525-4_lvl.json', fname
    print(f'✅ Long Run JSON (progressive): {j["workoutName"]}, 2 steps → {fname}')


def test_create_long_run_even():
    wkt = {**LONG_WORKOUT, 'strategy': 'even',
           'first_half_pace': '5:30', 'second_half_pace': None}
    j = create_garmin_workout(wkt, '4')
    steps = j['workoutSegments'][0]['workoutSteps']
    assert len(steps) == 1, f'Expected 1 step, got {len(steps)}'
    assert steps[0]['endConditionValue'] == 6000.0   # 100 min
    print(f'✅ Long Run JSON (even): 1 step, 100min')


def test_json_step_structure():
    """Verify targetValueOne/Two are top-level fields (not inside targetType)."""
    j = create_garmin_workout(INTERVAL_WORKOUT, '3', '3:45–4:00 мин/км')
    steps = j['workoutSegments'][0]['workoutSteps']
    # Tempo step
    s = steps[0]
    assert 'targetValueOne' in s, 'targetValueOne must be top-level'
    assert 'targetValueOne' not in s['targetType'], 'targetValueOne must NOT be inside targetType'
    assert s['targetType']['workoutTargetTypeKey'] == 'pace.zone'
    # Recovery step
    rec = steps[4]
    assert rec['stepType']['stepTypeKey'] == 'recovery'
    assert rec['targetValueOne'] is None
    assert rec['targetType']['workoutTargetTypeKey'] == 'no.target'
    # Inner steps have childStepId=1
    repeat = steps[5]
    assert repeat['workoutSteps'][0]['childStepId'] == 1
    assert repeat['workoutSteps'][1]['childStepId'] == 1
    # Interval inner step has v_fast > v_slow
    interval_inner = repeat['workoutSteps'][0]
    assert interval_inner['targetValueOne'] > interval_inner['targetValueTwo']
    print(f'✅ JSON step structure validated')


def test_garmin_json_interval():
    j = build_garmin_interval_workout(INTERVAL_WORKOUT, '3', '3:45–4:00 мин/км')
    assert j['workoutName']
    steps = j['workoutSegments'][0]['workoutSteps']
    repeat = next((s for s in steps if s.get('type') == 'RepeatGroupDTO'), None)
    assert repeat, f'No RepeatGroupDTO in: {[s.get("type") for s in steps]}'
    assert repeat['numberOfIterations'] == 10
    inner = repeat['workoutSteps']
    assert len(inner) == 2
    print(f'✅ Garmin JSON interval: {j["workoutName"]}, '
          f'main_steps={len(steps)}, reps={repeat["numberOfIterations"]}')


def test_garmin_json_interval_no_tempo():
    j = build_garmin_interval_workout(SIMPLE_INTERVALS, '3')
    steps = j['workoutSegments'][0]['workoutSteps']
    repeat = next((s for s in steps if s.get('type') == 'RepeatGroupDTO'), None)
    assert repeat and repeat['numberOfIterations'] == 8
    print(f'✅ Garmin JSON interval (no tempo): reps={repeat["numberOfIterations"]}')


def test_garmin_json_long_progressive():
    j = build_garmin_long_run_workout(LONG_WORKOUT, '4', 'progressive', '5:30', '5:00')
    steps = j['workoutSegments'][0]['workoutSteps']
    assert len(steps) == 2, f'Expected 2 steps, got {len(steps)}'
    assert steps[0]['endConditionValue'] == 3000.0   # 50*60
    print(f'✅ Garmin JSON long run (progressive): {j["workoutName"]}, steps={len(steps)}')


def test_garmin_json_long_even():
    j = build_garmin_long_run_workout(LONG_WORKOUT, '4', 'even', '5:30', None)
    steps = j['workoutSegments'][0]['workoutSteps']
    assert len(steps) == 1
    assert steps[0]['endConditionValue'] == 6000.0   # 100*60
    print(f'✅ Garmin JSON long run (even): steps=1, duration=100min')


def test_filename_format():
    assert workout_filename('2026-05-22', '3.5') == 'DD_20260522-3.5_lvl.json'
    assert interval_filename('2026-05-27', '3') == 'DD_20260527-3_lvl.json'
    assert long_run_filename('2026-05-25', '4') == 'DD_20260525-4_lvl.json'
    print('✅ Filename format OK')


if __name__ == '__main__':
    tests = [
        test_parse_group_block,
        test_parse_work_struct,
        test_interval_speeds,
        test_tempo_pace_list,
        test_create_interval_json,
        test_create_interval_json_grp35,
        test_create_interval_json_no_tempo,
        test_create_long_run_progressive,
        test_create_long_run_even,
        test_json_step_structure,
        test_garmin_json_interval,
        test_garmin_json_interval_no_tempo,
        test_garmin_json_long_progressive,
        test_garmin_json_long_even,
        test_filename_format,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f'❌ {t.__name__}: {e}')

    print(f'\n{"✅ All" if passed == len(tests) else f"⚠️ {passed}/{len(tests)}"} tests passed!')
