"""Safe single-session rescheduling over a *validated* existing timetable.

Scope deliberately narrow: every other session stays in its verified placement.
Candidate slot/room assignments are independently validated against the FULL
university workbook. CP-SAT ranks the verified options when available; it does
not invent feasibility or publish schedules.
"""
from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from northcap_cp_sat_solver import (
    build_sessions, clean, integer, make_slot_candidates, add_simulated_labs,
)
from timetable_policies import load_policies
from timetable_validator import validate, modeled_overlap_pairs
from timetable_fairness import quality_report


PASS = ('PASS', 'PASS_WITH_LIMITATIONS')


def csv_rows(content: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))


def csv_bytes(rows: list[dict], original_headers: list[str] | None = None) -> bytes:
    if not rows:
        raise ValueError('No timetable rows available')
    buffer = io.StringIO(newline='')
    columns = original_headers or list(rows[0])
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode('utf-8-sig')


def timetable_sessions(rows):
    by_id = defaultdict(list)
    for row in rows:
        by_id[clean(row.get('session_id'))].append(row)
    return by_id


def schedule_hash(csv_data: bytes) -> str:
    import hashlib
    # Normalize BOM/newlines and order of rows for stable identity.
    rows = csv_rows(csv_data)
    ordered = sorted((tuple(sorted((str(k), str(v)) for k, v in r.items())) for r in rows))
    return hashlib.sha256(repr(ordered).encode('utf-8')).hexdigest()


def _metrics(quality):
    q = quality.get('metrics', {})
    return {
        'faculty_gaps': int(q.get('teacher_gap_periods', 0)),
        'section_gaps': int(q.get('section_avoidable_gap_proxy', 0)),
        'overload': int(q.get('teacher_daily_overload_periods', 0)),
        'weekday_spread': int(q.get('total_teacher_weekday_load_spread', 0)),
        'late_periods': int(q.get('late_session_periods', 0)),
        'soft_avoid_periods': int(q.get('preferred_free_slot_periods', 0)),
    }


def penalty(metrics):
    """Transparent ranking heuristic, NOT a trained ML prediction."""
    return (200 * metrics['soft_avoid_periods'] + 40 * metrics['late_periods']
            + 3 * metrics['faculty_gaps'] + 5 * metrics['section_gaps']
            + 2 * metrics['overload'] + metrics['weekday_spread'])


def _swap(rows, sid, slot_records, room_id, days):
    updated = []
    replacement = sorted(slot_records, key=lambda x: integer(x['period']))
    idx = 0
    for row in rows:
        if clean(row['session_id']) == sid:
            slot = replacement[idx]
            idx += 1
            nxt = dict(row)
            nxt.update(day=clean(slot['day']), period=integer(slot['period']),
                       start_time=clean(slot['start_time']),
                       end_time=clean(slot['end_time']), room_id=room_id)
            updated.append(nxt)
        else:
            updated.append(dict(row))
    updated.sort(key=lambda r: (days.index(clean(r['day']).upper()), integer(r['period']),
                                     clean(r['group_id']), clean(r['session_id'])))
    return updated


def _slot_display(records):
    if not records:
        return ''
    return (f"{clean(records[0]['day'])} P{integer(records[0]['period'])}"
            + (f"–P{integer(records[-1]['period'])}" if len(records) > 1 else '')
            + f" ({clean(records[0]['start_time'])}–{clean(records[-1]['end_time'])})")


def _candidate_reasons(session, occupied_indices, slots, other_rows, overlapping, rooms, available_rooms):
    reasons = []
    pairs = {(clean(slots[ix]['day']).upper(), integer(slots[ix]['period'])) for ix in occupied_indices}
    for row in other_rows:
        if (clean(row['day']).upper(), integer(row['period'])) not in pairs:
            continue
        context = f"{clean(row['course_id'])} / {clean(row['group_id'])} / {clean(row['faculty_name'])}"
        if clean(row['faculty_id']) == session.faculty:
            reasons.append(f'Teacher already teaches {context}')
        if clean(row['group_id']) == session.group or frozenset((clean(row['group_id']), session.group)) in overlapping:
            reasons.append(f'Students/group overlap with {context}')
    if not available_rooms:
        reasons.append('No eligible classroom/laboratory is free for every period of this session')
    return list(dict.fromkeys(reasons))


def explain_disallowed_start(data, policies, session, groups, slots, requested):
    """Explain why the solver does not offer this start, using actual Excel rules."""
    from timetable_policies import effective_group_semester
    reasons = []
    first = next((i for i, sl in enumerate(slots) if clean(sl.get('slot_id')) == requested), None)
    if first is None:
        return ['Requested slot ID is not in this university workbook']
    if first + session.duration > len(slots):
        return ['The complete session does not fit in the remaining teaching slots']
    selected = slots[first:first + session.duration]
    periods = [integer(sl['period']) for sl in selected]
    if any(clean(sl['day']).upper() != clean(selected[0]['day']).upper() for sl in selected) \
            or periods != list(range(periods[0],periods[0]+session.duration)):
        reasons.append(f'This session needs {session.duration} continuous periods on one day')
    availability = {(clean(r.get('faculty_id')), clean(r.get('slot_id'))): clean(r.get('is_available')).lower()
                    in ('yes','true','y','1') for r in data.get('Faculty_Availability', [])}
    semester = effective_group_semester(session.group, groups)
    for sl in selected:
        slot_id = clean(sl['slot_id'])
        if clean(sl.get('academic_classes_allowed')).lower() not in ('yes','true','y','1'):
            reasons.append(f'{slot_id}: academic classes are not allowed')
        if slot_id in policies.hard_closed_slot_ids:
            reasons.append(f'{slot_id}: the institution has marked this slot hard-closed')
        if not availability.get((session.faculty,slot_id),False):
            reasons.append(f'{slot_id}: teacher {session.faculty} is unavailable in the Excel workbook')
        if policies.reserved(sl,semester,session.course in policies.window_allowed_course_codes):
            reasons.append(f'{slot_id}: blocked by the institutional open-elective reservation')
    return list(dict.fromkeys(reasons)) or ['Start time excluded by a current scheduling constraint']


def _choose(options):
    """Use a tiny CP-SAT selection model to rank independently verified moves.

    Deterministic numeric minimization; no LLM chooses a timetable.
    """
    if not options:
        return None, 'none'
    try:
        from ortools.sat.python import cp_model
        model = cp_model.CpModel()
        variables = [model.new_bool_var(f'valid_move_{i}') for i in range(len(options))]
        model.add_exactly_one(variables)
        model.minimize(sum(int(o['rank_cost']) * var for o, var in zip(options, variables)))
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 5
        solver.parameters.num_search_workers = 1
        status = solver.solve(model)
        if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            return next(i for i, var in enumerate(variables) if solver.value(var)), 'CP-SAT'
    except ImportError:
        pass
    return min(range(len(options)), key=lambda i: (options[i]['rank_cost'], options[i]['slot_id'], options[i]['room_id'])), 'verified-score-fallback'


def propose_moves(data: dict, baseline_rows: list[dict], session_id: str, *,
                  requested_slot_id: str | None = None, include_mentoring: bool = True,
                  extra_simulated_labs: int = 0, maximum_options: int = 12) -> dict:
    """Evaluate *only* one-session relocations; never edit source rows.

    Hard rules are checked twice: legal start candidates from the existing
    solver, then the independent validator for each complete proposed schedule.
    This catches lunch, daily limits, shared elective memberships and all
    conflicts not explicit in the local candidate enumerator.
    """
    if maximum_options < 1:
        raise ValueError('maximum_options must be positive')
    original = validate(data, baseline_rows, include_mentoring, extra_simulated_labs)
    if original['status'] not in PASS:
        return {'status': 'REJECTED_BASELINE', 'reasons': [x['message'] for x in original.get('errors', [])[:8]], 'options': []}
    session_id = clean(session_id)
    by_sid = timetable_sessions(baseline_rows)
    if session_id not in by_sid:
        return {'status': 'UNKNOWN_SESSION', 'reasons': ['The selected teaching session is not in the verified timetable'], 'options': []}
    policies = load_policies(data)
    sessions, courses, groups, faculty, warnings, errors = build_sessions(data, include_mentoring, policies)
    if errors:
        return {'status': 'INPUT_ERRORS', 'reasons': errors[:8], 'options': []}
    sessions_by_id = {session.sid: session for session in sessions}
    target = sessions_by_id.get(session_id)
    if target is None:
        return {'status': 'UNKNOWN_SESSION', 'reasons': ['Session not in course assignments'], 'options': []}
    slots, candidates, candidate_errors = make_slot_candidates(sessions, data, groups, policies)
    target_ix = next(i for i, session in enumerate(sessions) if session.sid == session_id)
    current = sorted(by_sid[session_id], key=lambda r: integer(r['period']))
    original_slot = next((clean(s['slot_id']) for s in slots
                          if clean(s['day']).upper() == clean(current[0]['day']).upper()
                          and integer(s['period']) == integer(current[0]['period'])), '')
    requested = clean(requested_slot_id)
    original_quality = quality_report(baseline_rows, groups, mode='before_move', policies=policies)
    original_metrics = _metrics(original_quality)
    before_penalty = penalty(original_metrics)
    baseline_unmodified = [r for r in baseline_rows if clean(r['session_id']) != session_id]
    busy_rooms = defaultdict(set)
    for r in baseline_unmodified:
        busy_rooms[(clean(r['day']).upper(), integer(r['period']))].add(clean(r['room_id']))
    room_inventory = list(data['Rooms'])
    if extra_simulated_labs:
        clone = add_simulated_labs({'Rooms': list(room_inventory)}, extra_simulated_labs)
        room_inventory = clone['Rooms']
    eligible = [r for r in room_inventory if clean(r['room_type']).lower() == target.room_type.lower()
                and integer(r.get('capacity')) >= target.students]
    overlap = {frozenset(pair) for pair in modeled_overlap_pairs(groups, {s.group for s in sessions})}
    valid = []
    blocked = Counter()
    reason_by_slot = {}
    # All current legal times from the actual solver. No unrecognized slots.
    allowed_start_ids = set()
    for start_idx, occupied in candidates[target_ix]:
        start_id = clean(slots[start_idx]['slot_id'])
        allowed_start_ids.add(start_id)
        if start_id == original_slot:
            continue
        period_pairs = [(clean(slots[ix]['day']).upper(), integer(slots[ix]['period'])) for ix in occupied]
        possible_rooms = [clean(r['room_id']) for r in eligible
                          if all(clean(r['room_id']) not in busy_rooms[pair] for pair in period_pairs)]
        reasons = _candidate_reasons(target, occupied, slots, baseline_unmodified, overlap,
                                      room_inventory, possible_rooms)
        if reasons:
            blocked.update(reasons)
            reason_by_slot[start_id] = reasons
            continue
        # A preferred room keeps the original room if it is available.
        original_room = clean(current[0]['room_id'])
        possible_rooms.sort(key=lambda r: (r != original_room, r))
        # Try one feasible room at a time; a complete independent validation
        # must pass. We only return one room recommendation per start slot.
        test_issues = []
        for room_id in possible_rooms:
            changed = _swap(baseline_rows, session_id, [slots[ix] for ix in occupied], room_id, policies.days)
            checked = validate(data, changed, include_mentoring, extra_simulated_labs)
            if checked['status'] in PASS:
                new_quality = quality_report(changed, groups, mode='proposed_move', policies=policies)
                scores = _metrics(new_quality)
                optionscore = penalty(scores)
                valid.append({
                    'slot_id': start_id, 'slot': _slot_display([slots[ix] for ix in occupied]),
                    'room_id': room_id, 'start_index': start_idx,
                    'requested': requested == start_id,
                    'metrics': scores,
                    'delta': {name: value-original_metrics[name] for name, value in scores.items()},
                    'rank_cost': optionscore + (0 if requested == start_id else (25 if requested else 0)),
                    'score_change': optionscore-before_penalty,
                    'rows': changed,
                    'validation': checked,
                })
                break
            test_issues.extend(str(x['message']) for x in checked.get('errors', [])[:3])
        if start_id not in {x['slot_id'] for x in valid}:
            reasons = list(dict.fromkeys(test_issues))[:4] or ['Independent validation rejected the proposed move']
            reason_by_slot[start_id] = reasons
            blocked.update(reasons)
    if requested and requested not in allowed_start_ids:
        if requested == original_slot:
            reason_by_slot[requested] = ['This is already the current time slot']
        else:
            reason_by_slot[requested] = explain_disallowed_start(
                data, policies, target, groups, slots, requested)
    valid.sort(key=lambda x: (x['rank_cost'], x['slot_id'], x['room_id']))
    # Preserve the explicitly requested candidate in top-N if it is feasible.
    top = valid[:maximum_options]
    if requested:
        requested_choice = next((x for x in valid if x['slot_id'] == requested), None)
        if requested_choice and requested_choice not in top:
            top[-1] = requested_choice
            top.sort(key=lambda x: (x['rank_cost'], x['slot_id']))
    winner, engine = _choose(top)
    return {
        'status': 'OPTIONS_FOUND' if top else 'NO_VALID_SINGLE_MOVE',
        'session_id': session_id,
        'course_id': target.course,
        'group_id': target.group,
        'faculty_id': target.faculty,
        'duration': target.duration,
        'current_slot': _slot_display(current),
        'current_slot_id': original_slot,
        'current_room': clean(current[0]['room_id']),
        'requested_slot_id': requested,
        'requested_reasons': reason_by_slot.get(requested, []),
        'other_reasons': blocked.most_common(6),
        'options': top,
        'recommended_index': winner,
        'selection_engine': engine,
        'baseline_metrics': original_metrics,
        'examined_valid_options': len(valid),
        'note': ('Single-session moves only. All other sessions remain unchanged. '
                 'Any changes must be explicitly approved by the coordinator. '
                 'Student-level conflicts not modeled in the workbook cannot be guaranteed.'),
    }


def public_options(proposal):
    """Return lightweight UI/table data without repeating the entire timetable."""
    return [{k: option[k] for k in ('slot_id', 'slot', 'room_id', 'requested', 'score_change', 'delta')}
            for option in proposal.get('options', [])]
