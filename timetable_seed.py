"""Safely reuse a validated standard timetable as a CP-SAT fairness search seed.

This module never changes Excel requirements or hard institutional constraints.
A seed is accepted only if the independent validator accepts every output row.
The seed is a *hint*, while most session starts are additionally fixed for a
small-neighborhood optimization. This reduces the work needed to rediscover a
feasible schedule, and does not itself claim any objective improvement.
"""
from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path


def read_verified_baseline(path, data, sessions, slots, candidates, include_mentoring=True):
    """Return validated baseline starts/rooms or raise ValueError.

    Reject stale or incompatible CSVs, even if they have plausible-looking rows.
    In particular, the session list, room assignments, academic calendar and
    policies must match the current workbook and mentor-week setting.
    """
    from timetable_validator import validate

    with Path(path).open(newline='', encoding='utf-8-sig') as file:
        rows = list(csv.DictReader(file))
    result = validate(data, rows, include_mentoring=include_mentoring)
    if result.get('status') not in ('PASS', 'PASS_WITH_LIMITATIONS'):
        issues = result.get('errors', [])
        raise ValueError('Standard timetable failed independent validation: '
                         + '; '.join(str(e) for e in issues[:3]))

    sid_to_rows = defaultdict(list)
    for row in rows:
        sid_to_rows[row.get('session_id', '')].append(row)
    if set(sid_to_rows) != {s.sid for s in sessions}:
        raise ValueError('Baseline session IDs differ from current Excel requirements')

    slot_ids = {(str(r['day']).strip().upper(), int(float(r['period']))): i
                for i, r in enumerate(slots)}
    starts = {}
    rooms = {}
    for i, sess in enumerate(sessions):
        block_rows = sid_to_rows[sess.sid]
        if len(block_rows) != sess.duration:
            raise ValueError(f'Baseline session {sess.sid} has wrong length')
        try:
            indices = sorted(slot_ids[(str(r['day']).strip().upper(), int(float(r['period'])))]
                             for r in block_rows)
        except (KeyError, ValueError) as exc:
            raise ValueError(f'Invalid baseline placement for {sess.sid}: {exc}') from exc
        options = [(first, occupied) for first, occupied in candidates[i]
                   if list(occupied) == indices]
        if len(options) != 1:
            raise ValueError(f'Baseline placement for {sess.sid} is not a current CP-SAT candidate')
        starts[sess.sid] = options[0][0]
        actual_rooms = {str(r['room_id']).strip() for r in block_rows}
        if len(actual_rooms) != 1:
            raise ValueError(f'Baseline room changes mid-session: {sess.sid}')
        rooms[sess.sid] = actual_rooms.pop()
    return starts, rooms


def choose_neighborhood(sessions, slots, starts, fraction=0.20, focus='balanced', groups=None,
                        operator=None, late_slot_ids=(), attempt=0):
    """Deterministically release the sessions most relevant to teacher gaps.

    The unselected sessions are fixed at independently validated baseline
    positions. The candidate set still allows all legal positions for released
    sessions. The baseline remains feasible under identical hard constraints.
    """
    from timetable_fairness import weights_for_profile, _parent_of
    weights_for_profile(focus)  # fail closed on unknown model recommendation
    operator_focuses = {
        'faculty_gap_repair': 'faculty_gaps',
        'section_gap_repair': 'section_gaps',
        'workload_balance_repair': 'daily_balance',
        'late_period_repair': 'late_period_repair',
    }
    if operator is not None and operator not in operator_focuses:
        raise ValueError('Unknown UCB1 neighborhood operator')
    selected_focus = operator_focuses[operator] if operator else focus
    if not 0 < fraction <= 1:
        raise ValueError('Neighborhood fraction must be in (0, 1]')
    if not sessions:
        return set()
    by_teacher_day = defaultdict(list)
    slot_map = {i: (str(s['day']), int(float(s['period']))) for i, s in enumerate(slots)}
    for s in sessions:
        day, period = slot_map[starts[s.sid]]
        by_teacher_day[(s.faculty, day)].extend(range(period, period + s.duration))
    gap_by_teacher = defaultdict(int)
    for (teacher, _), periods in by_teacher_day.items():
        covered = set(periods)
        gap_by_teacher[teacher] += max(0, max(covered) - min(covered) + 1 - len(covered))
    count = min(len(sessions), max(1, math.ceil(len(sessions) * fraction)))
    if selected_focus == 'section_gaps':
        # Prioritize groups with actual baseline gaps, keeping lab-batch parents
        # separate from unrelated/mixed elective enrolments.
        by_section_day = defaultdict(set)
        for sess in sessions:
            parent = _parent_of(sess.group, groups or {})
            if parent is None:
                continue
            day, period = slot_map[starts[sess.sid]]
            by_section_day[(parent, day)].update(range(period, period + sess.duration))
        section_gaps = defaultdict(int)
        for (section, _), periods in by_section_day.items():
            section_gaps[section] += max(0, max(periods) - min(periods) + 1 - len(periods))
        ranked = sorted(sessions, key=lambda s: (
            -section_gaps[_parent_of(s.group, groups or {})],
            -slot_map[starts[s.sid]][1], s.sid))
    elif selected_focus == 'daily_balance':
        by_teacher_days = defaultdict(lambda: defaultdict(set))
        for sess in sessions:
            day, period = slot_map[starts[sess.sid]]
            by_teacher_days[sess.faculty][day].update(range(period, period + sess.duration))
        teacher_overload = {}
        day_count = max(1, len({str(slot['day']) for slot in slots}))
        for teacher, days in by_teacher_days.items():
            threshold = math.ceil(sum(map(len, days.values())) / day_count)
            teacher_overload[teacher] = sum(max(0, len(p) - threshold) for p in days.values())
        ranked = sorted(sessions, key=lambda s: (
            -teacher_overload.get(s.faculty, 0),
            -slot_map[starts[s.sid]][1], s.sid))
    elif selected_focus == 'late_period_repair':
        late = set(str(x) for x in late_slot_ids)
        slot_name = {i: str(r.get('slot_id', '')) for i, r in enumerate(slots)}
        # Prioritize actual soft-late slots; keep deterministic fallback ordering.
        ranked = sorted(sessions, key=lambda s: (
            -int(slot_name[starts[s.sid]] in late),
            -slot_map[starts[s.sid]][1], s.sid))
    else:
        # Legacy baseline behavior for balanced and faculty-gap searches.
        ranked = sorted(sessions, key=lambda s: (
            -gap_by_teacher[s.faculty], -slot_map[starts[s.sid]][1], s.sid))
    if operator is not None and attempt > 1 and len(ranked) > count:
        # Retry the SAME strategy with a different, relevant neighborhood.
        # A rotated window of high-priority sessions avoids identical reruns.
        pool = ranked[:min(len(ranked), max(count + 1, math.ceil(1.75 * count)))]
        shift = ((int(attempt) - 1) * max(1, count // 3)) % len(pool)
        ranked = (pool[shift:] + pool[:shift]) + ranked[len(pool):]
    return {s.sid for s in ranked[:count]}


def seed_model(model, sessions, choices, seed_starts, released):
    """Apply *complete placement hints* and anchor non-neighborhood sessions.

    A hint cannot certify feasibility; the independent baseline validation and
    subsequent CP-SAT/final output checks remain mandatory.
    """
    if {s.sid for s in sessions} != set(seed_starts):
        raise ValueError('Incomplete seed session coverage')
    released = set(released)
    if not released <= set(seed_starts):
        raise ValueError('Unknown released session')
    fixed = 0
    for sess, opts in zip(sessions, choices):
        matching = [v for start, _, v in opts if start == seed_starts[sess.sid]]
        if len(matching) != 1:
            raise ValueError(f'No matching CP-SAT variable for baseline session {sess.sid}')
        selected = matching[0]
        for start, _, var in opts:
            model.add_hint(var, int(start == seed_starts[sess.sid]))
        if sess.sid not in released:
            model.add(selected == 1)
            fixed += 1
    return fixed
