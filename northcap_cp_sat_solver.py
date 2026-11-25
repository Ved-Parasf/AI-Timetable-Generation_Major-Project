"""Institution-configured CP-SAT timetable prototype.

Reads the supplied nine-sheet .xlsx directly with Python standard library.
Only extra package: ortools. Outputs CSV + HTML + a validation report.

Usage:
    python northcap_cp_sat_solver.py --excel "University_Timetable_Portable_Policy_Dataset.xlsx"
    python northcap_cp_sat_solver.py --excel "...xlsx" --validate-only

This is a PROTOTYPE, not an official validated university timetable.
"""
from __future__ import annotations

import argparse
import csv
import html
import math
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

XML_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
PACKAGE_REL_NS = '{http://schemas.openxmlformats.org/package/2006/relationships}'
VALID_COMPONENTS = {'THEORY_ONLY', 'PRACTICAL_ONLY', 'THEORY_AND_PRACTICAL'}
# Soft preference for preserving original Standard placements in target-free UCB1.
# This is a relative weight, NOT a hard number-of-classes cap.
ALNS_SOFT_DISRUPTION_WEIGHT = 4


def alns_disruption_weight(*, fairness_first=False, minimum_disruption=False):
    """Choose the phase-specific *soft* timetable-move cost.

    Before reaching the measured fairness goal, CP-SAT may use the ENTIRE
    coordinator-approved hard change budget without an additional per-move
    objective tax. After reaching the goal, prefer fewer original time moves.
    The separate cumulative change cap and all existing hard rules apply in
    both phases. Legacy/manual search retains its original weight of 6.
    """
    if fairness_first and minimum_disruption:
        raise ValueError('Fairness-first and minimum-disruption phases are exclusive')
    if fairness_first:
        return 0
    if minimum_disruption:
        return 80
    return 6


def excel_records(path: Path) -> dict[str, list[dict]]:
    """Read normal XLSX worksheets; supports shared strings and inline strings."""
    with ZipFile(path) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = ET.fromstring(z.read('xl/sharedStrings.xml'))
            for si in root.findall(XML_NS + 'si'):
                strings.append(''.join(n.text or '' for n in si.iter(XML_NS + 't')))
        workbook = ET.fromstring(z.read('xl/workbook.xml'))
        relationships = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        rels = {}
        for item in relationships.findall(PACKAGE_REL_NS + 'Relationship'):
            target = item.attrib['Target'].lstrip('/')
            if not target.startswith('xl/'):
                target = 'xl/' + target
            rels[item.attrib['Id']] = target
        out = {}
        for sh in workbook.find(XML_NS + 'sheets'):
            name = sh.attrib['name']
            rel_id = sh.attrib[REL_NS + 'id']
            xml = ET.fromstring(z.read(rels[rel_id]))
            table = []
            for row in xml.iter(XML_NS + 'row'):
                vals = {}
                for cell in row.findall(XML_NS + 'c'):
                    addr = cell.attrib.get('r', 'A1')
                    col = re.match('[A-Z]+', addr).group()
                    idx = 0
                    for ch in col:
                        idx = idx * 26 + (ord(ch) - ord('A') + 1)
                    idx -= 1
                    typ = cell.attrib.get('t', '')
                    v = cell.find(XML_NS + 'v')
                    if typ == 'inlineStr':
                        inline = cell.find(XML_NS + 'is')
                        value = ''.join(x.text or '' for x in inline.iter(XML_NS + 't')) if inline is not None else ''
                    elif v is None or v.text is None:
                        value = ''
                    elif typ == 's':
                        value = strings[int(v.text)]
                    elif typ == 'b':
                        value = v.text == '1'
                    elif typ in ('str', 'e'):
                        value = v.text
                    else:
                        try:
                            num = float(v.text)
                            value = int(num) if num.is_integer() else num
                        except ValueError:
                            value = v.text
                    vals[idx] = value
                if vals:
                    table.append([vals.get(j, '') for j in range(max(vals) + 1)])
            if not table:
                out[name] = []
                continue
            header = [str(v).strip() for v in table[0]]
            out[name] = []
            for row in table[1:]:
                row += [''] * max(0, len(header) - len(row))
                if not any(str(v).strip() for v in row):
                    continue
                out[name].append({h: v for h, v in zip(header, row) if h})
        return out


def clean(value) -> str:
    return '' if value is None else str(value).strip()


def true(value) -> bool:
    return clean(value).lower() in ('yes', 'true', '1', 'y')


def integer(value, default=0) -> int:
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def component_lt_p(course):
    parts = re.findall(r'\d+', clean(course.get('L-T-P', '')))
    if len(parts) != 3:
        raise ValueError(f"Invalid L-T-P for {course.get('course_id')}: {course.get('L-T-P')}")
    return tuple(map(int, parts))


@dataclass(frozen=True)
class Session:
    sid: str
    course: str
    group: str
    faculty: str
    kind: str
    duration: int
    room_type: str
    students: int
    source_assignment: str
    provisional: bool


def build_sessions(data, include_mentoring=True, policies=None):
    from timetable_policies import load_policies
    policies = policies or load_policies(data)
    courses = {clean(r['course_id']): r for r in data['Courses']}
    groups = {clean(r['group_id']): r for r in data['Student_Groups']}
    faculty = {clean(r['faculty_id']): r for r in data['Faculty']}
    sessions, warnings, errors = [], [], []
    seen = set()
    for a in data['Course_Assignments']:
        aid = clean(a.get('assignment_id'))
        course_id, gid, fid = map(lambda k: clean(a.get(k)), ('course_id', 'group_id', 'faculty_id'))
        comp = clean(a.get('scheduling_component'))
        status = clean(a.get('assignment_status'))
        if comp == 'DO_NOT_SCHEDULE' or 'NOT_SCHEDULE' in status or 'REFERENCE_ONLY' in status:
            continue
        is_mentor = (policies is not None and course_id in policies.mentor_courses
                     and policies.mentor_weeks > 0 and include_mentoring)
        if comp not in VALID_COMPONENTS and not is_mentor:
            if comp not in ('', 'DO_NOT_SCHEDULE'):
                warnings.append(f'{aid}: unknown component {comp}; skipped')
            continue
        if not fid or fid not in faculty or gid not in groups or course_id not in courses:
            errors.append(f'{aid}: invalid/missing faculty, group, or course: {fid}, {gid}, {course_id}')
            continue
        course = courses[course_id]
        l, t, p = component_lt_p(course)
        provisional = 'SYNTHETIC' in status or 'UNVERIFIED' in status or 'PROTOTYPE' in status
        students = integer(groups[gid].get('student_count'))
        if students <= 0:
            errors.append(f'{aid}: {gid} has no positive student_count')
            continue
        def add(kind, length, room_type, count):
            rt = clean(room_type).lower()
            if rt not in ('lab', 'classroom'):
                errors.append(f'{aid}: {course_id} {kind} needs a recognized room type; got {room_type!r}')
                return
            for n in range(count):
                key = (course_id, gid, fid, kind, n)
                if key in seen:
                    errors.append(f'{aid}: duplicate teaching session {key}')
                    continue
                seen.add(key)
                sessions.append(Session(f'{aid}_{kind[:1]}{n+1}', course_id, gid, fid,
                                        kind, length, rt.title(), students, aid, provisional))
        if is_mentor:
            # Week A includes a one-period mentor meeting; Week B omits it.
            add('MENTOR_FORTNIGHT', 1, 'Classroom', 1)
            continue
        if comp in ('THEORY_ONLY', 'THEORY_AND_PRACTICAL'):
            add('THEORY', 1, course.get('theory_room_type'), l)
            add('TUTORIAL', 1, course.get('theory_room_type'), t)
        if comp in ('PRACTICAL_ONLY', 'THEORY_AND_PRACTICAL'):
            blocks = integer(a.get('practical_blocks_week'), 0)
            if p and not blocks:
                warnings.append(f'{aid}: practical component has P={p} but 0 blocks; skipped')
            if p == 2:
                add('PRACTICAL', 2, course.get('practical_room_type'), blocks)
            elif p == 1:
                add('PRACTICAL', 1, course.get('practical_room_type'), blocks)
            elif p > 2:
                errors.append(f'{aid}: P={p} unsupported; verify how many continuous blocks are required')
    return sessions, courses, groups, faculty, warnings, errors


def group_memberships(groups):
    members = defaultdict(set)
    for gid, group in groups.items():
        kind = clean(group.get('group_kind'))
        parent = clean(group.get('parent_group_id'))
        if kind == 'PARENT':
            members[gid].add(gid)
        elif kind == 'LAB_BATCH':
            members[gid].add(parent)
        else:
            raw = clean(group.get('synthetic_membership_by_original_section'))
            for orig in raw.split(';'):
                orig = orig.split('=')[0].strip()
                if orig in groups:
                    if clean(groups[orig].get('group_kind')) == 'PARENT':
                        members[gid].add(orig)
                    elif clean(groups[orig].get('group_kind')) == 'LAB_BATCH':
                        parent_of_batch = clean(groups[orig].get('parent_group_id'))
                        if parent_of_batch in groups:
                            members[gid].add(parent_of_batch)
    # Cross-section membership comes solely from Student_Groups; no university
    # name or course code is inferred from a group ID.
    return members


def student_group_conflicts(groups, used_groups):
    """Pairs of groups known to share students. Different elective cohorts are
    assumed disjoint because the workbook has no individual student IDs."""
    parents = {g: clean(r.get('parent_group_id')) for g, r in groups.items()}
    kinds = {g: clean(r.get('group_kind')) for g, r in groups.items()}
    member_parents = group_memberships(groups)
    out = set()
    all_used = sorted(used_groups)
    raw_exact = {gid: {part.split('=')[0].strip() for part in clean(groups[gid].get('synthetic_membership_by_original_section')).split(';')}
                 for gid in all_used}
    for i, g1 in enumerate(all_used):
        for g2 in all_used[i + 1:]:
            k1, k2 = kinds[g1], kinds[g2]
            conflict = False
            if k1 == 'PARENT' and g1 in member_parents[g2]:
                conflict = True
            elif k2 == 'PARENT' and g2 in member_parents[g1]:
                conflict = True
            # A named lab batch may contain any elective enrollee from its section;
            # conservative, pending student-level enrollment data.
            elif k1 == 'LAB_BATCH' and k2 != 'LAB_BATCH' and parents[g1] in member_parents[g2]:
                named_batches = {x for x in raw_exact[g2] if kinds.get(x)=='LAB_BATCH' and parents.get(x)==parents[g1]}
                conflict = (not named_batches or g1 in named_batches)
            elif k2 == 'LAB_BATCH' and k1 != 'LAB_BATCH' and parents[g2] in member_parents[g1]:
                named_batches = {x for x in raw_exact[g1] if kinds.get(x)=='LAB_BATCH' and parents.get(x)==parents[g2]}
                conflict = (not named_batches or g2 in named_batches)
            # Shared theory and its own distinct language practical subgroups.
            elif clean(groups[g1].get('theory_delivery_group_id')) == g2 and g1 != g2:
                conflict = True
            elif clean(groups[g2].get('theory_delivery_group_id')) == g1 and g1 != g2:
                conflict = True
            if conflict:
                out.add((g1, g2))
    return out


def make_slot_candidates(sessions, data, groups, policies=None):
    from timetable_policies import load_policies
    policies = policies or load_policies(data)
    day_order = {day:i for i,day in enumerate(policies.days)}
    slots = sorted(data['Time_Slots'], key=lambda s: (day_order[clean(s['day']).upper()], integer(s['period'])))
    slot_by_id = {clean(s['slot_id']): j for j, s in enumerate(slots)}
    available = {(clean(r['faculty_id']), clean(r['slot_id'])): true(r['is_available'])
                 for r in data['Faculty_Availability']}
    result = []
    errors = []
    for s in sessions:
        group = groups[s.group]
        from timetable_policies import effective_group_semester
        semester = effective_group_semester(s.group, groups)
        # The policy engine selects the reservation mode from Excel.
        # Only explicitly declared Open Elective course IDs can be exempted
        # when REGULAR_CLASSES_BLOCKED is selected.
        candidate = []
        for ix, start in enumerate(slots):
            block = list(range(ix, ix + s.duration))
            if block[-1] >= len(slots):
                continue
            selected = [slots[j] for j in block]
            if any(clean(z['day']) != clean(start['day']) for z in selected):
                continue
            if any(integer(selected[j]['period']) != integer(start['period']) + j for j in range(s.duration)):
                continue
            if any(not true(z['academic_classes_allowed']) for z in selected):
                continue
            if s.kind == 'PRACTICAL' and policies.practical_exact_minutes is not None:
                # Exact clock minutes, not just the number of timetable periods.
                from timetable_policies import hm
                actual_minutes = sum(hm(z['end_time']) - hm(z['start_time']) for z in selected)
                adjacent = all(hm(a['end_time']) == hm(b['start_time'])
                               for a, b in zip(selected, selected[1:]))
                if actual_minutes != policies.practical_exact_minutes or not adjacent:
                    continue
            if any(clean(z['slot_id']) in policies.hard_closed_slot_ids for z in selected):
                continue
            if any(not available.get((s.faculty, clean(z['slot_id'])), False) for z in selected):
                continue
            is_declared_elective = s.course in policies.window_allowed_course_codes
            if any(policies.reserved(z, semester, is_declared_elective) for z in selected):
                continue
            candidate.append((ix, block))
        if not candidate:
            errors.append(f'{s.sid}: no allowed contiguous slot for {s.course} / {s.group} / {s.faculty}')
        result.append(candidate)
    return slots, result, errors


def analyze_capacity(sessions, data):
    rooms = data['Rooms']
    problems = []
    for s in sessions:
        eligible = [r for r in rooms if clean(r['room_type']).lower() == s.room_type.lower()
                    and integer(r['capacity']) >= s.students]
        if not eligible:
            problems.append(f'{s.sid}: no {s.room_type} accommodates {s.students} students ({s.course}, {s.group})')
    return problems


def required_room_capacity_audit(sessions, data, slots):
    """Prove simple resource shortfalls before calling CP-SAT.

    This is only a necessary condition, not proof of full feasibility.  Never
    rewrite the user's course requirements or silently fabricate rooms.
    """
    issues, lines = [], []
    by_kind = defaultdict(list)
    for s in sessions:
        by_kind[s.room_type].append(s)
    for room_type, reserved in sorted(by_kind.items()):
        rooms = [r for r in data['Rooms']
                 if clean(r.get('room_type')).lower() == room_type.lower()]
        usable = [s for s in slots if true(s.get('academic_classes_allowed'))]
        n_rooms, n_slots = len(rooms), len(usable)
        required_periods = sum(s.duration for s in reserved)
        maximum_periods = n_rooms * n_slots
        needed = math.ceil(required_periods / n_slots) if n_slots else 0
        lines.append(f'{room_type}: {required_periods} required room-periods; '
                     f'{n_rooms} rooms x {n_slots} allowed periods = '
                     f'{maximum_periods} available room-periods. '
                     f'Absolute minimum rooms by this bound: {needed}.')
        if required_periods > maximum_periods:
            shortfall = required_periods - maximum_periods
            issues.append(f'PROVEN {room_type.upper()} ROOM SHORTAGE: '
                          f'{required_periods} room-periods required but only '
                          f'{maximum_periods} available; shortage {shortfall} '
                          f'room-periods. At least {needed} '
                          f'{room_type.lower()} rooms are necessary (currently {n_rooms}). '
                          'Actual need may be greater due to teacher, student, '
                          'lunch, elective, and consecutive-period rules.')
        # When all of a type's sessions are exactly 2-period blocks, measure
        # independent maximum block count: disjoint pairs within each day.
        if reserved and all(s.duration == 2 for s in reserved):
            max_blocks_one_room = 0
            for day in dict.fromkeys(clean(s['day']) for s in usable):
                periods = sorted(integer(s['period']) for s in usable if clean(s['day']) == day)
                length, previous = 0, None
                for p in periods + [None]:
                    if p is not None and (previous is None or p == previous + 1):
                        length += 1
                    else:
                        max_blocks_one_room += length // 2
                        length = 1 if p is not None else 0
                    previous = p
            block_capacity = n_rooms * max_blocks_one_room
            lines.append(f'{room_type} 2-period blocks: {len(reserved)} required, '
                         f'at most {max_blocks_one_room} per room or '
                         f'{block_capacity} across {n_rooms} rooms.')
            if len(reserved) > block_capacity and required_periods <= maximum_periods:
                minimum = math.ceil(len(reserved) / max_blocks_one_room) if max_blocks_one_room else None
                issues.append(f'PROVEN {room_type.upper()} BLOCK SHORTAGE: '
                              f'{len(reserved)} consecutive two-period blocks '
                              f'required but only {block_capacity} fit into '
                              f'{n_rooms} rooms. At least {minimum} rooms necessary.')
    return issues, lines


def add_simulated_labs(data, count):
    """Explicit opt-in experiment; these rooms DO NOT exist in source Excel."""
    if count < 1:
        return data
    simulated = {name: list(rows) for name, rows in data.items()}
    for n in range(1, count + 1):
        simulated['Rooms'].append({
            'room_id': f'SYNTHETIC_LAB_{n:02}',
            'room_type': 'Lab', 'capacity': 30,
            'facilities': 'UNVERIFIED - PROTOTYPE ONLY',
            'source': 'SIMULATION ONLY - NOT a university room',
            'review_note': 'Do not submit this as a real university timetable.'
        })
    return simulated


def save_report(path, sessions, warnings, errors, additional=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = ['UNIVERSITY CP-SAT PRE-SOLVE REPORT', '=' * 45,
               f'Teaching sessions generated: {len(sessions)}',
               f'Theory periods: {sum(s.duration for s in sessions if s.kind == "THEORY")}',
               f'Practical blocks: {sum(1 for s in sessions if s.kind == "PRACTICAL")}',
               f'Fortnightly mentor meetings: {sum(1 for s in sessions if s.kind == "MENTOR_FORTNIGHT")}',
               f'Provisional sessions: {sum(s.provisional for s in sessions)}',
               '', 'ERRORS']
    content += errors or ['None']
    content += ['', 'WARNINGS'] + (warnings or ['None'])
    content += ['', 'SOLVER INFORMATION'] + list(additional)
    path.write_text('\n'.join(map(str, content)), encoding='utf-8')


def build_model(sessions, groups, faculty, data, slots, candidates, penalty=10,
                optimize_fairness=False, policies=None, fairness_priority='policy_first',
                quality_caps=None, fairness_profile='balanced',
                reference_starts=None, max_distinct_changes=None,
                disruption_weight=6, change_cost_theory=3, change_cost_practical=5):
    from ortools.sat.python import cp_model

    from timetable_policies import load_policies
    policies = policies or load_policies(data, optimize_fairness=optimize_fairness)
    days = policies.days
    model = cp_model.CpModel()
    # session -> possible (start, [occupied slot indexes], BoolVar)
    choices = []
    by_teacher_slot = defaultdict(list)
    by_teacher_day = defaultdict(list)
    by_teacher_day_period = defaultdict(list)
    by_group_slot = defaultdict(list)
    by_room_type_slot = defaultdict(list)
    by_room_type_slot_size = defaultdict(list)
    by_course_group_day = defaultdict(list)
    weekday = {i: clean(slots[i]['day']) for i in range(len(slots))}
    objective = []
    avoided_occupancies = []
    late_occupancies = []
    for si, session in enumerate(sessions):
        options = []
        for start_idx, indices in candidates[si]:
            v = model.new_bool_var(f'place_{si}_{start_idx}')
            options.append((start_idx, indices, v))
            for ix in indices:
                day = weekday[ix]
                period = integer(slots[ix]['period'])
                by_teacher_slot[(session.faculty, ix)].append(v)
                by_teacher_day[(session.faculty, day)].append(v)
                by_teacher_day_period[(session.faculty, day, period)].append(v)
                by_group_slot[(session.group, ix)].append(v)
                by_room_type_slot[(session.room_type, ix)].append(v)
                by_room_type_slot_size[(session.room_type, ix, session.students)].append(v)
                if clean(slots[ix]['slot_id']) in policies.avoid_slot_ids:
                    avoided_occupancies.append(v)
                if clean(slots[ix]['slot_id']) in policies.late_slot_ids:
                    late_occupancies.append(v)
                # Open Elective eligibility and placement are controlled by the
                # workbook policy mode, not by hardcoded language-course IDs.
            if session.kind == 'THEORY':
                by_course_group_day[(session.course, session.group, weekday[start_idx])].append(v)
        model.add_exactly_one([v for _, _, v in options])
        choices.append(options)

    for literals in by_teacher_slot.values():
        model.add_at_most_one(literals)
    for literals in by_group_slot.values():
        model.add_at_most_one(literals)
    # Presence of each named student group in every slot.
    presence = {}
    for (gid, ix), lits in by_group_slot.items():
        v = model.new_bool_var(f'group_{gid}_{ix}')
        model.add(sum(lits) == v)
        presence[(gid, ix)] = v
    used_groups = {s.group for s in sessions}
    for g1, g2 in student_group_conflicts(groups, used_groups):
        for ix in range(len(slots)):
            a, b = presence.get((g1, ix)), presence.get((g2, ix))
            if a is not None and b is not None:
                model.add(a + b <= 1)

    # Teachers: daily period limit, weekly clock-hour conversion, max consecutive slots.
    used_faculty = {s.faculty for s in sessions}
    for fid in used_faculty:
        rule = faculty[fid]
        max_daily = integer(rule.get('max_classes_per_day'), policies.faculty_daily_cap or 999)
        if policies.faculty_daily_cap is not None:
            max_daily = min(max_daily, policies.faculty_daily_cap)
        max_week_hours = float(rule.get('max_hours_per_week') or policies.faculty_weekly_hours_cap or 1000)
        if policies.faculty_weekly_hours_cap is not None:
            max_week_hours = min(max_week_hours, policies.faculty_weekly_hours_cap)
        # Each recorded slot may have a different duration at another university.
        # Weekly cap is in contact-minutes, not a hardcoded 50-minute conversion.
        max_week_minutes = math.floor(max_week_hours * 60 + 0.001)
        max_consecutive = integer(rule.get('max_consecutive_slots'), policies.faculty_consecutive_cap or 999)
        if policies.faculty_consecutive_cap is not None:
            max_consecutive = min(max_consecutive, policies.faculty_consecutive_cap)
        for day in days:
            assignments = by_teacher_day.get((fid, day), [])
            if assignments:
                model.add(sum(assignments) <= max_daily)
            day_periods = list(policies.periods_by_day[day])
            for i in range(max(0, len(day_periods) - max_consecutive)):
                window = day_periods[i:i+max_consecutive+1]
                if any(b != a+1 for a,b in zip(window,window[1:])):
                    continue
                segment = [v for p in window for v in by_teacher_day_period.get((fid, day, p), [])]
                if segment:
                    model.add(sum(segment) <= max_consecutive)
        week_minutes = []
        for (fac, ix), vs in by_teacher_slot.items():
            if fac != fid: continue
            from timetable_policies import hm
            slot = slots[ix]
            minutes = hm(slot['end_time']) - hm(slot['start_time'])
            week_minutes.extend(minutes * v for v in vs)
        model.add(sum(week_minutes) <= max_week_minutes)

    # No hidden 'one lecture per course per day' hard rule: add a declared
    # Institution_Policies row if a particular university requires this.

    # Room feasibility by room type/capacity threshold (actual room allocation is phase 2).
    rooms_by_type = defaultdict(list)
    for r in data['Rooms']:
        rooms_by_type[clean(r['room_type']).title()].append(integer(r['capacity']))
    for (room_type, ix), lits in by_room_type_slot.items():
        model.add(sum(lits) <= len(rooms_by_type[room_type]))
    for rt, capacities in rooms_by_type.items():
        for threshold in sorted(set(capacities))[:-1]:
            for ix in range(len(slots)):
                high = [v for (kind, jj, size), vs in by_room_type_slot_size.items()
                        if kind == rt and jj == ix and size > threshold for v in vs]
                if high:
                    model.add(sum(high) <= sum(c > threshold for c in capacities))

    # One free midday period (4/5/6) per parent section on each day.
    # The union avoids counting two simultaneous lab batches as two periods.
    memberships = group_memberships(groups)
    parents = [gid for gid, group in groups.items() if clean(group.get('group_kind')) == 'PARENT']
    for parent in parents:
        for day in days:
            period_bools = []
            for p in policies.lunch_periods:
                ix = next((j for j, sl in enumerate(slots) if clean(sl['day']) == day
                           and integer(sl['period']) == p), None)
                if ix is None:
                    continue
                applicable = [presence[(g, ix)] for g in used_groups
                              if (g == parent or clean(groups[g].get('parent_group_id')) == parent)
                              and (g, ix) in presence]
                if applicable:
                    occupy = model.new_bool_var(f'parent_{parent}_{day}_{p}')
                    model.add_max_equality(occupy, applicable)
                    period_bools.append(occupy)
            if period_bools:
                model.add(sum(period_bools) <= max(0, len(policies.lunch_periods) - policies.lunch_free_count))

    # A mixed elective student must also have a lunch period relative to the
    # original section. Enrollment is synthetic and subgroup identities unknown.
    electives = [g for g in used_groups if clean(groups[g].get('group_kind')).startswith('MIXED_')
                 or clean(groups[g].get('group_kind')) == 'SHARED_LANGUAGE_THEORY']
    for eg in electives:
        for par in memberships[eg]:
            for day in days:
                occ = []
                for p in policies.lunch_periods:
                    ix = next((j for j, sl in enumerate(slots) if clean(sl['day']) == day
                               and integer(sl['period']) == p), None)
                    if ix is None:
                        continue
                    base = [presence[(g, ix)] for g in used_groups
                            if (g == par or clean(groups[g].get('parent_group_id')) == par)
                            and (g, ix) in presence]
                    if (eg, ix) in presence:
                        base.append(presence[(eg, ix)])
                    if base:
                        v = model.new_bool_var(f'lunch_{eg}_{par}_{day}_{p}')
                        model.add_max_equality(v, base)
                        occ.append(v)
                if occ:
                    model.add(sum(occ) <= max(0, len(policies.lunch_periods) - policies.lunch_free_count))
    if quality_caps is not None:
        # Explicit comparison-only guards: caps come from the separately
        # generated, independently validated standard timetable.  They are
        # not changes to the institution's hard scheduling policy.
        if not optimize_fairness:
            raise ValueError('Quality guardrails require fairness optimization')
        for metric, values in [('avoid', avoided_occupancies),
                               ('late', late_occupancies)]:
            if metric in quality_caps:
                cap = int(quality_caps[metric])
                if cap < 0:
                    raise ValueError('Quality guardrails must be nonnegative')
                model.add(sum(values) <= cap)
    # For the current UCB1 mode, the ORIGINAL Standard is an anchor, not a
    # hard percentage cap. A change incurs a soft penalty; CP-SAT can still
    # move a class if that improves the combined fairness/stability objective.
    # Legacy explicit CLI budgets remain supported for archived experiments.
    time_change_terms = []
    weighted_time_change_terms = []
    if reference_starts is not None:
        if {s.sid for s in sessions} != set(reference_starts) or not optimize_fairness:
            raise ValueError('Complete validated Standard reference required for anchored fairness')
        for session, options in zip(sessions, choices):
            original = [var for start, _, var in options if start == reference_starts[session.sid]]
            if len(original) != 1:
                raise ValueError('Original Standard placement is not a valid candidate')
            time_change_terms.append(1-original[0])
            weighted_time_change_terms.append(
                (change_cost_practical if session.kind == 'PRACTICAL'
                 else change_cost_theory) * (1-original[0]))
        if max_distinct_changes is not None:
            if not 0 <= max_distinct_changes <= len(sessions):
                raise ValueError('Invalid legacy change budget')
            model.add(sum(time_change_terms) <= max_distinct_changes)
    elif max_distinct_changes is not None:
        raise ValueError('A change limit requires validated Standard placements')
    if optimize_fairness:
        # Soft-objective extension. Original hard scheduling constraints remain.
        from timetable_fairness import add_fairness_objective, priority_multipliers
        fair_terms = add_fairness_objective(model, sessions, groups, slots, choices,
                                            policies=policies, fairness_profile=fairness_profile,
                                            include_weekday_spread=False)  # Keep 4 short local solves tractable; validator still measures weekday spread.
        if fairness_priority == 'policy_first':
            avoid_scale, late_scale, fairness_bound = priority_multipliers(
                sessions, groups, slots, policies, fairness_profile=fairness_profile)
            if weighted_time_change_terms:
                # Preserve policy > late > fairness+disruption ordering even
                # after adding weighted session-change costs to the objective.
                extra_bound = disruption_weight * len(sessions) * max(
                    change_cost_theory, change_cost_practical)
                late_scale = fairness_bound + extra_bound + 1
                avoid_scale = (sum(s.duration for s in sessions) * late_scale
                               + fairness_bound + extra_bound + 1)
                if avoid_scale * sum(s.duration for s in sessions) > (2**62):
                    raise ValueError('Fairness objective coefficients exceed CP-SAT bounds')
            # Highest: fewer avoided-policy slots. Then: fewer late periods.
            # Finally: fairness gaps and daily load distribution.
            # Only enabled nonzero Excel soft penalties activate each tier.
            model.minimize(
                (avoid_scale * sum(avoided_occupancies) if policies.avoid_weight > 0 else 0)
                + (late_scale * sum(late_occupancies) if policies.late_weight > 0 else 0)
                + sum(fair_terms)
                + disruption_weight * sum(weighted_time_change_terms))
        elif fairness_priority == 'guarded_fairness':
            # UCB1-specific objective. Baseline soft-policy and late-slot caps
            # are enforced as separate hard model constraints above, so the
            # search can focus on measured fairness rather than spending its
            # short time budget reducing already-protected avoidance scores.
            # A small penalty prefers a cheaper move when gains are comparable.
            if quality_caps is None or 'avoid' not in quality_caps or 'late' not in quality_caps:
                raise ValueError('Guarded fairness requires both measured Standard policy caps')
            model.minimize(10 * sum(fair_terms) + disruption_weight *
                           sum(weighted_time_change_terms))
        elif fairness_priority == 'balanced':
            model.minimize(policies.avoid_weight * sum(avoided_occupancies)
                           + policies.late_weight * sum(late_occupancies)
                           + sum(fair_terms)
                           + disruption_weight * sum(weighted_time_change_terms))
        else:
            raise ValueError(f'Unknown fairness priority: {fairness_priority}')
    else:
        # The standard CP-SAT baseline retains Excel-defined numeric weights.
        model.minimize(policies.avoid_weight * sum(avoided_occupancies)
                       + policies.late_weight * sum(late_occupancies))
    return model, choices


def allocate_rooms(sessions, slot_choice, slots, rooms, seconds=30, preferred_rooms=None,
                   reference_starts=None, reference_rooms=None, max_distinct_changes=None,
                   room_change_cost=1):
    """Exact second CP-SAT pass: choose one capacity-compatible room for each
    fixed-time session, enforcing no overlapping room reservations."""
    from ortools.sat.python import cp_model
    model = cp_model.CpModel()
    by_room_slot = defaultdict(list)
    choices = []
    time_changed = set()
    if max_distinct_changes is not None:
        if (reference_starts is None or reference_rooms is None
                or {s.sid for s in sessions} != set(reference_rooms)):
            raise ValueError('Missing original Standard rooms for cumulative change cap')
        time_changed = {s.sid for s, (start, _) in zip(sessions, slot_choice)
                        if start != reference_starts[s.sid]}
        if len(time_changed) > max_distinct_changes:
            return None, 'More session times changed than the allowed Standard disruption budget'
    room_change_terms = []
    for i, s in enumerate(sessions):
        _, indices = slot_choice[i]
        alternatives = []
        for room in rooms:
            if clean(room['room_type']).lower() != s.room_type.lower():
                continue
            if integer(room['capacity']) < s.students:
                continue
            rid = clean(room['room_id'])
            v = model.new_bool_var(f'r_{i}_{rid.replace(" ", "_")}')
            alternatives.append((rid, v))
            for ix in indices:
                by_room_slot[(rid, ix)].append(v)
        if not alternatives:
            return None, f'No eligible room for {s.sid}'
        model.add_exactly_one([v for _, v in alternatives])
        if preferred_rooms and s.sid in preferred_rooms:
            preferred = preferred_rooms[s.sid]
            for rid, v in alternatives:
                model.add_hint(v, int(rid == preferred))
        if reference_rooms is not None and s.sid in reference_rooms:
            original_room = str(reference_rooms[s.sid])
            original = [var for rid, var in alternatives if rid == original_room]
            if max_distinct_changes is not None and s.sid not in time_changed:
                if len(original) != 1:
                    return None, f'Original room unavailable for unchanged session {s.sid}'
                room_change_terms.append(1-original[0])
            elif max_distinct_changes is None and len(original) == 1:
                # Prefer retaining even the original room; no fixed cap.
                room_change_terms.append(1-original[0])
        choices.append(alternatives)
    for lits in by_room_slot.values():
        model.add_at_most_one(lits)
    if max_distinct_changes is not None:
        # Legacy explicit budget: count room-only changes not already counted
        # by a time move. This path is not used by the current UCB1 UI.
        model.add(sum(room_change_terms) <= max_distinct_changes-len(time_changed))
    if room_change_terms:
        model.minimize(max(1, room_change_cost) * sum(room_change_terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_search_workers = 4
    status = solver.solve(model)
    if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        return None, f'Room assignment status {solver.status_name(status)}'
    return [next(rid for rid, v in alts if solver.value(v)) for alts in choices], None


def write_csv(path, columns, rows):
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def write_html_timetables(path, rows, groups, faculty, slots, policies=None, *,
                          dataset_name=None, university_name=None, timetable_type=None, rooms=()):
    """Build a polished standalone timetable and per-section course directory.

    This is a presentation-only change. Scheduling, constraints, validation
    and solver outputs are unchanged.
    """
    from timetable_report import render_timetable_html
    reserved = policies.reserved_slot_ids if policies is not None else ()
    min_sem = policies.elective_min_sem if policies is not None else 999
    allowed = policies.window_allowed_course_codes if policies is not None else ()
    mode = policies.elective_window_mode if policies is not None else 'DISABLED'
    path.write_text(render_timetable_html(
        rows, groups, slots, reserved_elective_slot_ids=reserved,
        elective_min_semester=min_sem, elective_courses=allowed,
        elective_window_mode=mode, fixed_non_teaching_blocks=(
            policies.fixed_non_teaching_blocks if policies is not None else ()),
        dataset_name=dataset_name,
        university_name=university_name, timetable_type=timetable_type, rooms=rooms), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='Generate CP-SAT timetables from an institution-configured Excel workbook')
    parser.add_argument('--excel', required=True, type=Path, help='Path to the supplied XLSX workbook')
    parser.add_argument('--output', type=Path, default=Path('generated_timetable'))
    parser.add_argument('--dataset-name', default=None, help='Original Excel filename, for report display only')
    parser.add_argument('--university-name', default=None, help='Institution label for report display only')
    parser.add_argument('--seconds', type=int, default=120, help='Time limit for teaching-slot allocation')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--optimize-fairness', action='store_true',
                        help='Add soft teacher/section gap, day-load and late-period preferences')
    parser.add_argument('--fairness-focus', choices=['balanced', 'faculty_gaps', 'section_gaps', 'daily_balance'],
                        default='balanced', help='Bounded fairness objective/seed focus, optionally selected by verified AI advice')
    parser.add_argument('--fairness-priority', choices=['policy_first', 'balanced', 'guarded_fairness'],
                        default='policy_first', help='Policy-first protects avoided/late slots before gap fairness; balanced uses Excel soft coefficients')
    parser.add_argument('--max-avoided-periods', type=int, default=None,
                        help='Comparison-only cap, measured from independently validated standard baseline')
    parser.add_argument('--max-late-periods', type=int, default=None,
                        help='Comparison-only cap, measured from independently validated standard baseline')
    parser.add_argument('--seed-timetable', type=Path, default=None,
                        help='Previously independently validated STANDARD CSV for anchored fairness search')
    parser.add_argument('--standard-reference', type=Path, default=None,
        help='Original independently validated Standard CSV; used only for disruption-aware ALNS')
    parser.add_argument('--max-distinct-changes', type=int, default=None,
        help='Absolute cumulative unique-session change budget versus Standard')
    parser.add_argument('--change-cost-theory', type=int, default=3,
        help='Prototype disruption penalty for moving a theory/mentor session (ALNS only)')
    parser.add_argument('--change-cost-practical', type=int, default=5,
        help='Prototype disruption penalty for moving a practical block (ALNS only)')
    parser.add_argument('--change-cost-room', type=int, default=1,
        help='Prototype disruption penalty for a room-only reassignment (ALNS only)')
    parser.add_argument('--minimum-disruption-phase', action='store_true',
        help='Emphasize unchanged Standard placements once measured fairness target is reached')
    parser.add_argument('--fairness-first-phase', action='store_true',
        help='Before the measured goal, optimize fairness without an additional soft time-move tax; the cumulative hard change limit remains')
    parser.add_argument('--alns-operator', choices=[
        'faculty_gap_repair', 'section_gap_repair',
        'workload_balance_repair', 'late_period_repair'], default=None,
        help='Bounded UCB1 neighborhood strategy; only valid with independently validated seed')
    parser.add_argument('--alns-attempt', type=int, default=0,
        help='Deterministic diversification index for the chosen search operator')
    parser.add_argument('--room-seconds', type=int, default=30,
        help='Maximum time for final room assignment; default preserves existing behavior')
    parser.add_argument('--neighborhood-fraction', type=float, default=0.20,
                        help='Fraction of baseline sessions allowed to move (default 0.20)')
    parser.add_argument('--exclude-mentoring', action='store_true', help='Create week B without fortnightly mentor meetings')
    parser.add_argument('--prototype-extra-labs', type=int, default=0, metavar='N',
                        help='SIMULATION ONLY: add N fictitious 30-seat labs in memory; never edits Excel')
    args = parser.parse_args()
    if not args.excel.exists():
        sys.exit(f'Excel file not found: {args.excel}')
    args.output.mkdir(parents=True, exist_ok=True)
    print('Reading Excel:', args.excel)
    try:
        data = excel_records(args.excel)
    except Exception as exc:
        sys.exit(f'Cannot read Excel: {exc}')
    required = ['Faculty', 'Courses', 'Student_Groups', 'Course_Assignments', 'Rooms',
                'Time_Slots', 'Faculty_Availability', 'Institution_Policies']
    missing = [s for s in required if s not in data]
    if missing:
        sys.exit(f'Missing Excel sheets: {missing}')
    if (not all(1 <= value <= 30 for value in
                (args.change_cost_theory, args.change_cost_practical, args.change_cost_room))):
        print('Error: change costs must be positive integers up to 30')
        return 2
    try:
        alns_disruption_weight(fairness_first=args.fairness_first_phase,
                               minimum_disruption=args.minimum_disruption_phase)
    except ValueError as exc:
        print('Error:', exc)
        return 2
    if args.fairness_priority == 'guarded_fairness' and not (
            args.optimize_fairness and args.alns_operator and args.standard_reference
            and args.seed_timetable
            and args.max_avoided_periods is not None and args.max_late_periods is not None):
        print('Error: guarded fairness requires a baseline-verified UCB1 run')
        return 2
    if (args.minimum_disruption_phase or args.fairness_first_phase) and (
            args.standard_reference is None or args.alns_operator is None or
            args.seed_timetable is None or not args.optimize_fairness):
        print('Error: AI Fairness phases require the validated original Standard timetable, '
              'an adaptive neighborhood, and Fairness optimization')
        return 2
    if not 0 <= args.prototype_extra_labs <= 10:
        print('Error: --prototype-extra-labs must be between 0 and 10')
        return 2
    if args.prototype_extra_labs:
        data = add_simulated_labs(data, args.prototype_extra_labs)
        print(f'!!! SIMULATION ONLY: Added {args.prototype_extra_labs} synthetic labs (30 seats). '
              'These are NOT real university rooms and do not modify the Excel.', flush=True)
    from timetable_policies import load_policies
    policies = load_policies(data, optimize_fairness=args.optimize_fairness)
    policies.export(args.output)
    if args.exclude_mentoring and policies.mentor_weeks == 1:
        policies.errors.append('Weekly mandatory mentoring (every 1 week) cannot be excluded using --exclude-mentoring.')
        policies.export(args.output)
    sessions, courses, groups, faculty, warnings, errors = build_sessions(
        data, include_mentoring=not args.exclude_mentoring, policies=policies
    )
    errors.extend(policies.errors)
    warnings.extend(policies.warnings)
    slots, candidates, candidate_errors = make_slot_candidates(sessions, data, groups, policies=policies)
    errors.extend(candidate_errors)
    errors.extend(analyze_capacity(sessions, data))
    capacity_errors, capacity_lines = required_room_capacity_audit(sessions, data, slots)
    errors.extend(capacity_errors)
    penalty = policies.avoid_weight
    warnings.append('Provisional/synthetic faculty and student memberships are assumed, not university-verified.')
    warnings.append('Different language cohorts are assumed to have disjoint enrolled students; exact student IDs are absent.')
    warnings.append('Policies are read from enabled Institution_Policies rows. Check policy_compliance.txt for data-checked and conditional policies.')
    if args.prototype_extra_labs:
        warnings.insert(0, f'SIMULATION ONLY: {args.prototype_extra_labs} fictitious 30-seat laboratory room(s) added. '
                             'Any timetable is NOT valid against the original university rooms.')
    report = args.output / 'validation_report.txt'
    save_report(report, sessions, warnings, errors, ['ROOM CAPACITY AUDIT'] + capacity_lines)
    print(f'Sessions: {len(sessions)}, errors: {len(errors)}. Report: {report}')
    if errors:
        print('Cannot solve until pre-solve errors are corrected. First five:')
        for e in errors[:5]:
            print(' -', e)
        return 2
    if args.validate_only:
        print('Pre-solve checks passed. Run without --validate-only to generate timetable.')
        return 0
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        sys.exit('OR-Tools is missing. Run: python -m pip install ortools')
    caps = {}
    if args.fairness_focus != 'balanced' and not args.optimize_fairness:
        print('Error: --fairness-focus requires --optimize-fairness')
        return 2
    if (args.alns_operator is not None and (args.seed_timetable is None or not args.optimize_fairness)):
        print('Error: --alns-operator requires --seed-timetable and --optimize-fairness')
        return 2
    if args.max_distinct_changes is not None and args.standard_reference is None:
        print('Error: legacy disruption cap requires original Standard reference')
        return 2
    if args.standard_reference is not None and args.alns_operator is None:
        print('Error: anchored Standard reference requires adaptive neighborhood search')
        return 2
    if args.max_distinct_changes is not None and args.max_distinct_changes < 0:
        print('Error: legacy change cap must be nonnegative')
        return 2
    if args.alns_attempt < 0 or args.room_seconds < 1:
        print('Error: ALNS attempt and room time bounds are invalid')
        return 2
    if args.seed_timetable is not None and not 0 < args.neighborhood_fraction <= 1:
        print('Error: --neighborhood-fraction must be in (0, 1]')
        return 2
    if args.max_avoided_periods is not None:
        caps['avoid'] = args.max_avoided_periods
    if args.max_late_periods is not None:
        caps['late'] = args.max_late_periods
    if caps and not args.optimize_fairness:
        print('Quality caps require --optimize-fairness')
        return 2
    if any(v < 0 for v in caps.values()):
        print('Quality caps must be nonnegative')
        return 2
    # Validate the STANDARD timetable against this exact workbook before using
    # its placements as hints or restricting the local fairness neighborhood.
    seed_starts, seed_rooms, released = None, None, set()
    reference_starts, reference_rooms = None, None
    if args.seed_timetable is not None:
        if not args.optimize_fairness:
            print('Error: --seed-timetable requires --optimize-fairness')
            return 2
        from timetable_seed import read_verified_baseline, choose_neighborhood
        try:
            seed_starts, seed_rooms = read_verified_baseline(
                args.seed_timetable, data, sessions, slots, candidates,
                include_mentoring=not args.exclude_mentoring)
            released = choose_neighborhood(sessions, slots, seed_starts,
                                           fraction=args.neighborhood_fraction,
                                           focus=args.fairness_focus, groups=groups,
                                           operator=args.alns_operator,
                                           late_slot_ids=policies.late_slot_ids,
                                           attempt=args.alns_attempt)
        except (OSError, ValueError) as exc:
            print('REJECTED baseline timetable:', exc, flush=True)
            return 2
    if args.standard_reference is not None:
        from timetable_seed import read_verified_baseline
        try:
            reference_starts, reference_rooms = read_verified_baseline(
                args.standard_reference, data, sessions, slots, candidates,
                include_mentoring=not args.exclude_mentoring)
            if (args.max_distinct_changes is not None
                    and args.max_distinct_changes > len(sessions)):
                raise ValueError('Legacy change budget cannot exceed session count')
        except (OSError, ValueError) as exc:
            print('REJECTED Standard timetable reference:', exc, flush=True)
            return 2
    model, choices = build_model(sessions, groups, faculty, data, slots, candidates, penalty,
                                 optimize_fairness=args.optimize_fairness, policies=policies,
                                 fairness_priority=args.fairness_priority,
                                 quality_caps=(caps or None),
                                 fairness_profile=args.fairness_focus,
                                 reference_starts=reference_starts,
                                 max_distinct_changes=args.max_distinct_changes,
                                 change_cost_theory=args.change_cost_theory,
                                 change_cost_practical=args.change_cost_practical,
                                 disruption_weight=(ALNS_SOFT_DISRUPTION_WEIGHT if args.fairness_priority == 'guarded_fairness'
                                                    and not (args.fairness_first_phase or args.minimum_disruption_phase)
                                                    else alns_disruption_weight(
                                     fairness_first=args.fairness_first_phase,
                                     minimum_disruption=args.minimum_disruption_phase)))
    if args.fairness_priority == 'guarded_fairness' and not (args.fairness_first_phase or args.minimum_disruption_phase):
        print('Adaptive fairness: preserve Standard placements where useful; '
              'soft stability penalty (no fixed change-percentage limit or fairness goal).', flush=True)
    elif args.fairness_first_phase:
        print('Legacy fairness-first phase enabled.', flush=True)
    elif args.minimum_disruption_phase:
        print('Legacy minimum-disruption phase enabled.', flush=True)
    if seed_starts is not None:
        from timetable_seed import seed_model
        fixed = seed_model(model, sessions, choices, seed_starts, released)
        print(f'Validated baseline seed: {len(sessions)} sessions; '
              f'{fixed} starts fixed; {len(released)} released for local improvement.',
              flush=True)
    if args.optimize_fairness:
        print('Fairness priority mode:', args.fairness_priority, flush=True)
        print('Fairness focus profile:', args.fairness_focus, flush=True)
        if caps:
            print('Validated baseline comparison caps:', caps, flush=True)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(1, args.seconds)
    solver.parameters.num_search_workers = max(1, args.workers)
    print(f'Solving {len(sessions)} sessions with CP-SAT ...', flush=True)
    status = solver.solve(model)
    status_name = solver.status_name(status)
    print('CP-SAT status:', status_name)
    if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        message = f'{status_name}: no verified timetable was produced. Do not treat this as a valid schedule.'
        if status == cp_model.UNKNOWN:
            message += (' Local improvement did not establish a feasible result in time; '
                        'the validated STANDARD timetable remains available.'
                        if seed_starts is not None else
                        ' Try --seconds 300. UNKNOWN is not proof of infeasibility.')
        if status == cp_model.INFEASIBLE:
            message += ' Hard constraints conflict; inspect room loads, faculty limits, lunch and elective windows.'
        save_report(report, sessions, warnings, errors, ['ROOM CAPACITY AUDIT'] + capacity_lines + [message])
        print(message)
        return 3
    assignment = []
    for i, options in enumerate(choices):
        winner = next((start, occ) for start, occ, v in options if solver.value(v))
        assignment.append(winner)
    print('Assigning actual rooms ...', flush=True)
    room_ids, room_error = allocate_rooms(sessions, assignment, slots, data['Rooms'],
                                          preferred_rooms=seed_rooms, seconds=args.room_seconds,
                                          reference_starts=reference_starts,
                                          reference_rooms=reference_rooms,
                                          max_distinct_changes=args.max_distinct_changes,
                                          room_change_cost=args.change_cost_room)
    if room_error:
        save_report(report, sessions, warnings, errors,
                    ['ROOM CAPACITY AUDIT'] + capacity_lines +
                    [f'First-pass timetable found, but NO VALID ROOM ALLOCATION: {room_error}'])
        print('Room allocation failed:', room_error)
        return 4
    rows = []
    for i, session in enumerate(sessions):
        first, occupied = assignment[i]
        for j in occupied:
            slot = slots[j]
            rows.append({'session_id': session.sid, 'assignment_id': session.source_assignment,
                         'day': clean(slot['day']), 'period': integer(slot['period']),
                         'start_time': clean(slot['start_time']), 'end_time': clean(slot['end_time']),
                         'course_id': session.course, 'course_name': clean(courses[session.course]['course_name']),
                         'kind': session.kind, 'group_id': session.group,
                         'faculty_id': session.faculty, 'faculty_name': clean(faculty[session.faculty]['faculty_name']),
                         'room_id': room_ids[i], 'students': session.students,
                         'provisional': 'YES' if session.provisional else 'NO'})
    rows.sort(key=lambda r: (policies.days.index(r['day']), r['period'], r['group_id'], r['course_id']))
    # Independent, fail-closed P02–P05 audit on final occupied periods.
    # Never publish a noncompliant timetable, even if other checks missed it.
    from timetable_policies import check_elective_reservation
    elective_problems = check_elective_reservation(data, rows, policies)
    if elective_problems:
        messages = [f"{x['error']}: {x.get('course_id')} / {x.get('group_id')} "
                    f"at {x.get('day')} period {x.get('period')}"
                    for x in elective_problems]
        save_report(report, sessions, warnings, errors,
                    ['OPEN ELECTIVE HARD-CONSTRAINT VALIDATION FAILED'] + messages)
        print('Cannot publish invalid timetable:', messages[:8])
        return 5
    if args.max_distinct_changes is not None:
        from timetable_bandit_alns import final_placement_audit
        import io
        stream = io.StringIO()
        fields = list(rows[0]) if rows else []
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        final_audit, _ = final_placement_audit(
            args.standard_reference.read_bytes(), stream.getvalue().encode('utf-8-sig'))
        if final_audit['distinct_changed_sessions'] > args.max_distinct_changes:
            print('DISRUPTION BUDGET FAILED: publication blocked', final_audit, flush=True)
            return 5
        print(f"Cumulative disruption audit: {final_audit['distinct_changed_sessions']}"
              f' / {args.max_distinct_changes} distinct sessions changed from Standard.', flush=True)
    fields = list(rows[0]) if rows else []
    write_csv(args.output / 'timetable_all.csv', fields, rows)
    write_csv(args.output / 'timetable_faculty.csv', fields,
              sorted(rows, key=lambda r: (r['faculty_name'], policies.days.index(r['day']), r['period'])))
    write_csv(args.output / 'timetable_rooms.csv', fields,
              sorted(rows, key=lambda r: (r['room_id'], policies.days.index(r['day']), r['period'])))
    write_csv(args.output / 'timetable_groups.csv', fields,
              sorted(rows, key=lambda r: (r['group_id'], policies.days.index(r['day']), r['period'])))
    from timetable_identity import suggested_university_name
    write_html_timetables(args.output / 'timetable_sections.html', rows, groups, faculty, slots,
                          rooms=data['Rooms'], policies=policies, dataset_name=args.dataset_name or args.excel.name,
                          university_name=(args.university_name or suggested_university_name(
                              args.dataset_name or args.excel.name)),
                          timetable_type='Fairness Optimized' if args.optimize_fairness else 'Standard CP-SAT')
    # Independent checks on the finished CSV rows.
    violations = []
    for attr in ('faculty_id', 'room_id', 'group_id'):
        usage = Counter((r[attr], r['day'], r['period']) for r in rows)
        for key, n in usage.items():
            if n > 1:
                violations.append(f'Overlapping {attr}: {key}, {n} simultaneous sessions')
    # Independently check every pair of student groups that share students.
    student_conflicts = student_group_conflicts(groups, {s.group for s in sessions})
    occupied_groups = defaultdict(set)
    for r in rows:
        occupied_groups[(r['day'], r['period'])].add(r['group_id'])
    for (day, period), gs in occupied_groups.items():
        for g1, g2 in student_conflicts:
            if g1 in gs and g2 in gs:
                violations.append(f'Shared student overlap: {g1} / {g2} at {day} period {period}')
    # Each continuous practical block must remain consecutive in one room.
    practical_check = defaultdict(list)
    for r in rows:
        if r['kind'] == 'PRACTICAL':
            practical_check[r['session_id']].append(r)
    for sid, block in practical_check.items():
        block = sorted(block, key=lambda r: r['period'])
        if len(set(r['day'] for r in block)) != 1 or len(set(r['room_id'] for r in block)) != 1:
            violations.append(f'Practical block {sid} spans days or rooms')
        if len(block) == 2 and block[1]['period'] != block[0]['period'] + 1:
            violations.append(f'Practical block {sid} is not consecutive')
    if violations:
        # This must never silently pass as a valid timetable.
        save_report(report, sessions, warnings, errors,
                    ['ROOM CAPACITY AUDIT'] + capacity_lines + ['FAILED FINAL VALIDATION'] + violations)
        print('FINAL VALIDATION FAILED:', violations[:5])
        return 5
    # Quality metrics are computed only from a valid *finished* timetable.
    # Reporting makes no claim that a run with limited search time is optimal.
    from timetable_fairness import quality_report, save_quality_files
    quality = quality_report(rows, groups,
                             'fairness' if args.optimize_fairness else 'standard', policies=policies,
                             fairness_profile=args.fairness_focus if args.optimize_fairness else 'balanced')
    quality['optimizer_priority_mode'] = (args.fairness_priority if args.optimize_fairness else 'standard')
    quality['optimizer_fairness_focus'] = (args.fairness_focus if args.optimize_fairness else 'balanced')
    quality['baseline_comparison_caps'] = (caps or None)
    quality['excel_soft_policy_weights'] = {
        'avoided_period_penalty': policies.avoid_weight,
        'late_period_penalty': policies.late_weight,
    }
    save_quality_files(args.output, quality, write_csv)
    save_report(report, sessions, warnings, errors,
                ['ROOM CAPACITY AUDIT'] + capacity_lines +
                ([f'SIMULATION-ONLY RESULT: {args.prototype_extra_labs} synthetic labs were added; '
                  'not valid against university room inventory'] if args.prototype_extra_labs else []) +
                [f'CP-SAT STATUS: {status_name}', f'Objective: {solver.objective_value}',
                 f'Scheduled sessions: {len(sessions)}', f'Scheduled slot rows: {len(rows)}',
                 f'Fairness optimization: {"ON" if args.optimize_fairness else "OFF"}',
                 f'Fairness priority: {args.fairness_priority if args.optimize_fairness else "standard"}',
                 f'Baseline caps (comparison only): {caps or "none"}',
                 f'Faculty gap periods: {quality["metrics"]["teacher_gap_periods"]}',
                 f'Section avoidable gap proxy: {quality["metrics"]["section_avoidable_gap_proxy"]}',
                 'Teacher, room and direct student-group overlaps: 0',
                 'One two-period practical occupies the SAME room on BOTH output CSV rows.',
                 'Week A includes one mentor session per section; Week B uses --exclude-mentoring.'])
    print('Timetable generated successfully!')
    if args.prototype_extra_labs:
        print('WARNING: SIMULATED rooms were used. This is NOT a validated university timetable.')
    print('Open:', args.output / 'timetable_sections.html')
    print('Excel-compatible file:', args.output / 'timetable_all.csv')
    print('Verification:', report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
