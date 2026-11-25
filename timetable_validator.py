"""Independent post-solve timetable validator for Timora Workspace.

Reads the source Excel and the actual *timetable_all.csv*, not the HTML.
Does not modify the solver/model. It rechecks generated placements against
requirements reconstructed from Course_Assignments and Courses.

Limitations: the XLSX contains synthetic aggregate elective memberships,
not individual student IDs. This checker cannot prove individual-level clashes
between different elective groups. Also, input validity != institutional truth.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path

from northcap_cp_sat_solver import excel_records
from timetable_policies import load_policies, hm, effective_group_semester

KINDS = {'THEORY_ONLY', 'PRACTICAL_ONLY', 'THEORY_AND_PRACTICAL'}


def txt(v):
    return '' if v is None else str(v).strip()


def yes(v):
    return txt(v).lower() in ('1','true','yes','y')


def whole(v, default=0):
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class Problem:
    code: str
    message: str
    session: str = ''


def modeled_memberships(groups):
    """Reconstruct modeled source-section membership from Excel records."""
    out=defaultdict(set)
    for gid, g in groups.items():
        kind=txt(g.get('group_kind'))
        if kind=='PARENT':
            out[gid].add(gid)
        elif kind=='LAB_BATCH':
            parent=txt(g.get('parent_group_id'))
            if parent: out[gid].add(parent)
        else:
            for item in txt(g.get('synthetic_membership_by_original_section')).split(';'):
                parent=item.split('=')[0].strip()
                if parent in groups:
                    if txt(groups[parent].get('group_kind'))=='PARENT':out[gid].add(parent)
                    elif txt(groups[parent].get('group_kind'))=='LAB_BATCH':
                        original=txt(groups[parent].get('parent_group_id'))
                        if original in groups:out[gid].add(original)
    return out


def modeled_overlap_pairs(groups, used_groups):
    """Pair conflicts derived from workbook group scopes, separate from solver."""
    parents={gid:txt(g.get('parent_group_id')) for gid,g in groups.items()}
    kinds={gid:txt(g.get('group_kind')) for gid,g in groups.items()}
    memberships=modeled_memberships(groups)
    out=set()
    names=sorted(used_groups)
    raw_exact={gid:{part.split('=')[0].strip() for part in txt(groups[gid].get('synthetic_membership_by_original_section')).split(';')} for gid in names}
    for i,g1 in enumerate(names):
        for g2 in names[i+1:]:
            conflict=(
                kinds.get(g1)=='PARENT' and g1 in memberships.get(g2,set())
                or kinds.get(g2)=='PARENT' and g2 in memberships.get(g1,set())
                or kinds.get(g1)=='LAB_BATCH' and kinds.get(g2)!='LAB_BATCH' and parents[g1] in memberships.get(g2,set())
                or kinds.get(g2)=='LAB_BATCH' and kinds.get(g1)!='LAB_BATCH' and parents[g2] in memberships.get(g1,set())
                or txt(groups[g1].get('theory_delivery_group_id'))==g2 and g1!=g2
                or txt(groups[g2].get('theory_delivery_group_id'))==g1 and g1!=g2
            )
            if conflict and kinds.get(g1)=='LAB_BATCH' and kinds.get(g2)!='LAB_BATCH':
                named={x for x in raw_exact[g2] if kinds.get(x)=='LAB_BATCH' and parents.get(x)==parents.get(g1)}
                if named and g1 not in named:conflict=False
            if conflict and kinds.get(g2)=='LAB_BATCH' and kinds.get(g1)!='LAB_BATCH':
                named={x for x in raw_exact[g1] if kinds.get(x)=='LAB_BATCH' and parents.get(x)==parents.get(g2)}
                if named and g2 not in named:conflict=False
            if conflict: out.add((g1,g2))
    return out


def build_expected(data, include_mentoring=True, policies=None):
    """Rebuild expected session IDs separately from the solver's session list."""
    policies = policies or load_policies(data)
    courses = {txt(x['course_id']):x for x in data['Courses']}
    faculty = {txt(x['faculty_id']):x for x in data['Faculty']}
    groups = {txt(x['group_id']):x for x in data['Student_Groups']}
    expected = {}
    issues = []
    for a in data['Course_Assignments']:
        aid, cid, gid, fid = (txt(a.get(k)) for k in ('assignment_id','course_id','group_id','faculty_id'))
        comp, status = txt(a.get('scheduling_component')), txt(a.get('assignment_status'))
        if comp == 'DO_NOT_SCHEDULE' or 'NOT_SCHEDULE' in status or 'REFERENCE_ONLY' in status:
            continue
        mentor = cid in policies.mentor_courses and policies.mentor_weeks > 0 and include_mentoring
        if not mentor and comp not in KINDS:
            continue
        if cid not in courses or gid not in groups or fid not in faculty:
            issues.append(Problem('INPUT_REFERENCE', f'{aid}: invalid course/group/faculty reference'))
            continue
        course = courses[cid]
        parts = re.findall(r'\d+', txt(course.get('L-T-P')))
        if len(parts) != 3:
            issues.append(Problem('INPUT_LTP', f'{aid}: malformed L-T-P'))
            continue
        l, t, p = map(int, parts)
        if mentor:
            instructions = [('MENTOR_FORTNIGHT', 1, 'Classroom', 1)]
        else:
            instructions = []
            if comp in ('THEORY_ONLY','THEORY_AND_PRACTICAL'):
                instructions.extend([('THEORY', 1, txt(course.get('theory_room_type')), l),
                                     ('TUTORIAL', 1, txt(course.get('theory_room_type')), t)])
            if comp in ('PRACTICAL_ONLY','THEORY_AND_PRACTICAL'):
                blocks = whole(a.get('practical_blocks_week'))
                if p not in (0,1,2):
                    issues.append(Problem('INPUT_LTP', f'{aid}: unsupported practical duration P={p}'))
                if p in (1,2):
                    instructions.append(('PRACTICAL', p, txt(course.get('practical_room_type')), blocks))
        for kind, duration, room_type, count in instructions:
            for i in range(1,count+1):
                sid = f'{aid}_{kind[0]}{i}'
                if sid in expected:
                    issues.append(Problem('DUPLICATE_REQUIREMENT', f'{sid}: repeated expected identifier', sid))
                expected[sid] = {
                    'course_id':cid, 'group_id':gid, 'faculty_id':fid,
                    'kind':kind, 'duration':duration, 'room_type':room_type.lower(),
                    'students':whole(groups[gid].get('student_count')),
                }
    return expected, courses, groups, faculty, issues


def validate(data, rows, include_mentoring=True, extra_simulated_labs=0):
    """Return a JSON-safe report with errors, warnings, metrics and conclusions."""
    policies = load_policies(data)
    days = policies.days
    lunch = policies.lunch_periods
    expected, courses, groups, teachers, errors = build_expected(data, include_mentoring, policies=policies)
    errors = list(errors) + [Problem('POLICY_ERROR', msg) for msg in policies.errors]
    warnings = list(policies.warnings)
    if extra_simulated_labs:
        warnings.append(f'{extra_simulated_labs} fictitious labs were added in memory by the solver. This is simulation data.')
    rooms = {txt(r.get('room_id')):r for r in data.get('Rooms', [])}
    for i in range(1, extra_simulated_labs+1):
        rid = f'SYNTHETIC_LAB_{i:02}'
        if rid in rooms:
            errors.append(Problem('DUPLICATE_SIMULATED_ROOM',f'Room {rid} already in workbook; simulated lab ID collision'))
        else:
            rooms[rid] = {'room_id':rid,'room_type':'Lab','capacity':30}
    if any(r.startswith('SYNTHETIC_LAB') for r in rooms):
        warnings.append('Synthetic rooms are included in the inventory; a pass is for the prototype, not proof of real campus capacity.')
    if any('SYNTHETIC' in txt(a.get('assignment_status')).upper() or 'UNVERIFIED' in txt(a.get('assignment_status')).upper() or 'PROTOTYPE' in txt(a.get('assignment_status')).upper() for a in data.get('Course_Assignments', [])):
        warnings.append('Provisional/synthetic teaching allocations exist; their real-world correctness is not established by validation.')
    warnings.append('Language elective memberships are aggregate, synthetic section counts without individual student IDs. Exact student-level overlap between different elective cohorts CANNOT be verified.')
    slots = {(txt(s.get('day')),whole(s.get('period'))):s for s in data['Time_Slots']}
    faculty_availability = {(txt(r.get('faculty_id')),txt(r.get('slot_id'))):yes(r.get('is_available')) for r in data['Faculty_Availability']}
    occupancy_faculty, occupancy_room, occupancy_group = defaultdict(set),defaultdict(set),defaultdict(set)
    by_session = defaultdict(list)
    problems_seen = set()
    def error(code, message, sid=''):
        key = (code,message,sid)
        if key not in problems_seen:
            errors.append(Problem(code,message,sid));problems_seen.add(key)

    for ix, row in enumerate(rows, 2):
        sid = txt(row.get('session_id'))
        if not sid:
            error('MISSING_SESSION_ID',f'CSV line {ix} has no session_id');continue
        if sid not in expected:
            error('UNEXPECTED_SESSION',f'{sid}: not required by Course_Assignments',sid)
            continue
        exp = expected[sid]
        by_session[sid].append(row)
        day, period = txt(row.get('day')).upper(),whole(row.get('period'))
        slot = slots.get((day,period))
        for key in ('course_id','group_id','faculty_id','kind'):
            if txt(row.get(key)) != exp[key]:
                error('WRONG_ASSIGNMENT',f'{sid}: {key}={txt(row.get(key))!r}, expected {exp[key]!r}',sid)
        if whole(row.get('students'),-1) != exp['students']:
            error('STUDENT_COUNT',f'{sid}: students mismatch from Student_Groups',sid)
        if not slot:
            error('UNKNOWN_SLOT',f'{sid}: {day} period {period} not in Time_Slots',sid)
        else:
            if not yes(slot.get('academic_classes_allowed')) or txt(slot.get('slot_id')) in policies.hard_closed_slot_ids:
                error('CLOSED_SLOT',f'{sid}: assigned to blocked {day} period {period}',sid)
            if txt(row.get('start_time')) != txt(slot.get('start_time')) or txt(row.get('end_time')) != txt(slot.get('end_time')):
                error('SLOT_TIME',f'{sid}: CSV times differ from Time_Slots at {day} period {period}',sid)
            if not faculty_availability.get((exp['faculty_id'],txt(slot.get('slot_id'))),False):
                error('FACULTY_UNAVAILABLE',f'{sid}: faculty unavailable {day} period {period}',sid)
            sem = effective_group_semester(exp['group_id'], groups)
            # Verify the same Excel-driven mode used to build candidates.
            is_declared_elective = exp['course_id'] in policies.window_allowed_course_codes
            if policies.reserved(slot, sem, is_declared_elective):
                error('ELECTIVE_RESERVATION',f'{sid}: class violates Open Elective mode {policies.elective_window_mode}, {day} period {period}',sid)
        room_id = txt(row.get('room_id'))
        if room_id not in rooms:
            error('UNKNOWN_ROOM',f'{sid}: room {room_id!r} is not in inventory',sid)
        else:
            room = rooms[room_id]
            if txt(room.get('room_type')).lower() != exp['room_type']:
                error('ROOM_TYPE',f'{sid}: {room_id} has wrong type for {exp["kind"]}',sid)
            if whole(room.get('capacity')) < exp['students']:
                error('ROOM_CAPACITY',f'{sid}: {room_id} seats {whole(room.get("capacity"))}, needs {exp["students"]}',sid)
        pair = (day,period)
        for table, ident, code in ((occupancy_faculty,exp['faculty_id'],'TEACHER_CLASH'),
                                    (occupancy_room,room_id,'ROOM_CLASH'),
                                    (occupancy_group,exp['group_id'],'GROUP_CLASH')):
            if (ident, *pair) in table and sid not in table[(ident,*pair)]:
                others = sorted(table[(ident,*pair)])
                error(code,f'{ident}: {day} period {period} overlaps sessions {others} and {sid}',sid)
            table[(ident,*pair)].add(sid)

    # Coverage and continuous room reservations.
    for sid, exp in expected.items():
        entries = by_session.get(sid,[])
        if not entries:
            error('MISSING_SESSION',f'{sid}: {exp["course_id"]} / {exp["group_id"]} has not been scheduled',sid)
            continue
        if len(entries) != exp['duration']:
            error('WRONG_DURATION',f'{sid}: expected {exp["duration"]} occupied periods; found {len(entries)}',sid)
        keys = [(txt(x.get('day')).upper(),whole(x.get('period'))) for x in entries]
        if len(set(keys)) != len(keys):
            error('DUPLICATE_OUTPUT_ROW',f'{sid}: repeated day/period CSV row',sid)
        if len({txt(x.get('day')).upper() for x in entries}) != 1:
            error('SPLIT_SESSION',f'{sid}: a single session spans multiple days',sid)
        if len({txt(x.get('room_id')) for x in entries}) != 1:
            error('ROOM_CHANGE',f'{sid}: a continuous session changes room',sid)
        if exp['kind'] == 'PRACTICAL' and policies.practical_exact_minutes is not None and entries:
            clock_slots = [slots.get((txt(e.get('day')).upper(), whole(e.get('period'))))
                           for e in entries]
            if all(clock_slots):
                clock_slots = sorted(clock_slots, key=lambda sl: hm(sl['start_time']))
                actual_minutes = sum(hm(sl['end_time']) - hm(sl['start_time'])
                                     for sl in clock_slots)
                contiguous = all(hm(a['end_time']) == hm(b['start_time'])
                                 for a,b in zip(clock_slots, clock_slots[1:]))
                if actual_minutes != policies.practical_exact_minutes or not contiguous:
                    error('EXACT_PRACTICAL_DURATION',
                          f'{sid}: practical occupies {actual_minutes} clock minutes '
                          f'(required {policies.practical_exact_minutes}), or spans a time gap', sid)
        if len(entries) == exp['duration'] and exp['duration'] > 1:
            periods = sorted(whole(x.get('period')) for x in entries)
            if periods != list(range(periods[0], periods[0]+exp['duration'])):
                error('NONCONSECUTIVE',f'{sid}: {exp["duration"]}-period session is not consecutive',sid)

    # Shared-student groups (the known overlaps in the Excel section membership model).
    group_pair_conflicts = modeled_overlap_pairs(groups, {x['group_id'] for x in expected.values()})
    for g1,g2 in sorted(group_pair_conflicts):
        for day in days:
            for period in policies.periods_by_day[day]:
                if occupancy_group.get((g1,day,period)) and occupancy_group.get((g2,day,period)):
                    error('SHARED_STUDENT_CLASH',f'{g1} and {g2}: overlapping {day} period {period}')

    # Faculty limits, spread of same theory course, and lunch windows.
    faculty_periods = defaultdict(set)
    class_course_days = defaultdict(Counter)
    for sid, entries in by_session.items():
        exp = expected[sid]
        if exp['kind'] == 'THEORY' and entries:
            day = txt(entries[0].get('day')).upper()
            class_course_days[(exp['course_id'],exp['group_id'])][day] += 1
        for row in entries:
            day = txt(row.get('day')).upper(); p=whole(row.get('period'))
            faculty_periods[(exp['faculty_id'],day)].add(p)
    # No hidden one-theory-per-course-per-day policy: it isn't declared in Excel.
    for fid, record in teachers.items():
        all_week=set()
        for day in days:
            periods=faculty_periods.get((fid,day),set())
            all_week.update((day,p) for p in periods)
            max_day=whole(record.get('max_classes_per_day'),policies.faculty_daily_cap or 999)
            if policies.faculty_daily_cap is not None:max_day=min(max_day,policies.faculty_daily_cap)
            if len(periods)>max_day:
                error('DAILY_FACULTY_LOAD',f'{fid}: {len(periods)} periods on {day}, limit {max_day}')
            max_seq=whole(record.get('max_consecutive_slots'),policies.faculty_consecutive_cap or 999)
            if policies.faculty_consecutive_cap is not None:max_seq=min(max_seq,policies.faculty_consecutive_cap)
            if max_seq>0:
                seq=0
                for p in policies.periods_by_day[day]:
                    seq = seq+1 if p in periods else 0
                    if seq>max_seq:
                        error('CONSECUTIVE_FACULTY_LOAD',f'{fid}: exceeds {max_seq} consecutive periods on {day}')
                        break
        try:
            max_week_hours=float(record.get('max_hours_per_week') or policies.faculty_weekly_hours_cap or 1000)
            if policies.faculty_weekly_hours_cap is not None:
                max_week_hours=min(max_week_hours,policies.faculty_weekly_hours_cap)
            total_minutes=sum(hm(slots[(day,p)]['end_time'])-hm(slots[(day,p)]['start_time']) for day,p in all_week)
            if total_minutes > math.floor(max_week_hours*60+0.001):
                error('WEEKLY_FACULTY_LOAD',f'{fid}: {total_minutes} minutes exceeds {max_week_hours} clock hours')
        except (TypeError,ValueError,KeyError) as exc:
            error('WEEKLY_FACULTY_LOAD_CHECK',f'{fid}: could not evaluate weekly hours: {exc}')

    # Lunch windows are taken from enabled Institution_Policies rows.
    group_occupied = {(g,d,p) for (g,d,p), sessions in occupancy_group.items() if sessions}
    parent_sections=[gid for gid, info in groups.items() if txt(info.get('group_kind'))=='PARENT']
    for par in parent_sections:
        child={par} | {gid for gid, g in groups.items() if txt(g.get('parent_group_id'))==par}
        for day in days:
            blocked=[p for p in lunch if any((g,day,p) in group_occupied for g in child)]
            if lunch and len(blocked)>len(lunch)-policies.lunch_free_count:
                error('LUNCH_BREAK',f'{par}: insufficient lunch periods on {day} (policy window {lunch})')
    memberships=modeled_memberships(groups)
    for eg, record in groups.items():
        if txt(record.get('group_kind')).startswith('MIXED_') or txt(record.get('group_kind'))=='SHARED_LANGUAGE_THEORY':
            for par in memberships.get(eg,set()):
                base={par} | {gid for gid,g in groups.items() if txt(g.get('parent_group_id'))==par}
                for day in days:
                    busy=[p for p in lunch if ((eg,day,p) in group_occupied or any((g,day,p) in group_occupied for g in base))]
                    if lunch and len(busy)>len(lunch)-policies.lunch_free_count:
                        error('ELECTIVE_LUNCH',f'{eg} / {par}: insufficient shared lunch on {day}')

    avoid_pairs={(txt(s.get('day')).upper(),whole(s.get('period'))) for sid,s in policies.slot_index.items() if sid in policies.avoid_slot_ids}
    thu_period_rows=[r for r in rows if (txt(r.get('day')).upper(), whole(r.get('period'))) in avoid_pairs]
    counters=Counter(issue.code for issue in errors)
    return {
        'status':'FAIL' if errors else 'PASS_WITH_LIMITATIONS',
        'metrics':{
            'expected_sessions':len(expected),
            'scheduled_sessions':sum(bool(by_session.get(x)) for x in expected),
            'csv_rows':len(rows),
            'expected_slot_rows':sum(x['duration'] for x in expected.values()),
            'hard_violations':len(errors),
            'thursday_afternoon_occupied_rows':len(thu_period_rows), # compatibility name; policy-avoided rows
            'policy_avoided_occupied_rows':len(thu_period_rows),
            'assumed_room_count':sum(r.startswith('SYNTHETIC_LAB') for r in rooms),
        },
        'counts_by_error':dict(counters),
        'errors':[asdict(x) for x in errors],
        'warnings':warnings,
        'scope_note':('Checks the timetable CSV against course assignments, time slots, room inventory, faculty availability, '
                      'teaching limits, consecutive blocks, elective windows, lunch and modeled group overlaps. '
                      'No individual student IDs are supplied; unmodeled clashes cannot be ruled out.'),
    }


def validate_bytes(workbook_bytes, csv_bytes, include_mentoring=True, extra_simulated_labs=0):
    data=excel_records(io.BytesIO(workbook_bytes))
    rows=list(csv.DictReader(io.StringIO(csv_bytes.decode('utf-8-sig'))))
    return validate(data,rows,include_mentoring,extra_simulated_labs)


def report_as_text(report):
    m=report['metrics']
    lines=['TIMORA WORKSPACE | INDEPENDENT POST-SOLVE VALIDATION', '='*57,
           'Status: '+report['status'],
           f'Required sessions: {m["expected_sessions"]}',
           f'Scheduled sessions: {m["scheduled_sessions"]}',
           f'Expected occupied periods: {m["expected_slot_rows"]}',
           f'Output CSV period rows: {m["csv_rows"]}',
           f'Hard violations: {m["hard_violations"]}',
           f'Occupied periods in policy soft-avoid slots (not a hard error): {m["policy_avoided_occupied_rows"]}',
           '', 'SCOPE',report['scope_note'],'','ERRORS']
    lines += [f'- {x["code"]}: {x["message"]}' for x in report['errors'][:500]] or ['None']
    lines += ['','WARNINGS'] + ['- '+x for x in report['warnings']]
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser(description='Independently validate generated CSV against Excel dataset')
    parser.add_argument('--excel',required=True,type=Path)
    parser.add_argument('--csv',required=True,type=Path)
    parser.add_argument('--output',type=Path,default=Path('independent_validation.txt'))
    parser.add_argument('--week-b',action='store_true',help='Omit fortnightly mentoring sessions')
    parser.add_argument('--extra-labs',type=int,default=0)
    args=parser.parse_args()
    report=validate_bytes(args.excel.read_bytes(),args.csv.read_bytes(),not args.week_b,args.extra_labs)
    args.output.write_text(report_as_text(report),encoding='utf-8')
    args.output.with_suffix('.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(report_as_text(report)[:3500]);print('Saved:',args.output)
    return 0 if not report['errors'] else 2

if __name__=='__main__':
    raise SystemExit(main())
