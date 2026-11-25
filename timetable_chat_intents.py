"""High-confidence, read-only timetable questions that must never inherit a move target.

Every answer is computed from the selected timetable and workbook. Groq may
select other read-only tools but never supplies rooms, conflicts or changes.
"""
from __future__ import annotations

from collections import defaultdict
import re

import timetable_assistant as facts


def _groups(data, rows):
    ids = {facts.norm(g.get('group_id')) for g in data.get('Student_Groups', [])}
    ids.update(facts.norm(r.get('group_id')) for r in rows)
    return {x: {} for x in ids if x}


def _faculty_names(data, rows):
    return {facts.norm(x.get('faculty_name')) for x in data.get('Faculty', [])} | {
        facts.norm(x.get('faculty_name')) for x in rows}


def _valid_course(question, rows, data):
    catalog = {facts.norm(c.get('course_id')): c for c in data.get('Courses', [])}
    return facts.match_course_reference(question, catalog, rows)


def _course_times(question, rows, data):
    """Answer both halves of 'who teaches CSL214N and when?' in one turn."""
    q = facts.normalized(question)
    if not (re.search(r'\b(who|teachers?|faculty|instructor)\b', q)
            and re.search(r'\b(teach|teaches|taught|handles?)\b', q)
            and re.search(r'\b(when|what time|which day|at what time)\b', q)):
        return None
    course, ambiguous = _valid_course(question, rows, data)
    if ambiguous:
        return 'Multiple course IDs match this subject: ' + ', '.join(ambiguous) + '. Which code do you mean?'
    if not course:
        return 'Please give the exact course code or subject title to list its teachers and their class times.'
    by_teacher = defaultdict(dict)
    for r in rows:
        if facts.norm(r.get('course_id')) != course:
            continue
        name = facts.norm(r.get('faculty_name'))
        if not name:
            continue
        sid = facts.norm(r.get('session_id'))
        key = sid or (r.get('group_id'), r.get('day'), r.get('period'), r.get('kind'))
        by_teacher[name].setdefault(key, []).append(r)
    if not by_teacher:
        return f'No scheduled teachers found for **{course}**.'
    title = next((facts.norm(r.get('course_name')) for r in rows
                  if facts.norm(r.get('course_id')) == course), '')
    days = {'MON':0,'TUE':1,'WED':2,'THU':3,'FRI':4,'SAT':5,'SUN':6}
    clock_slots={(facts.norm(sl.get('day')).upper(), facts.int_value(sl.get('period'))): sl
                 for sl in data.get('Time_Slots', [])}
    lines = []
    for name, sessions in sorted(by_teacher.items()):
        all_sessions = []
        for block in sessions.values():
            ordered = sorted(block, key=lambda r: facts.int_value(r.get('period')))
            first, last = ordered[0], ordered[-1]
            p1, p2 = facts.int_value(first.get('period')), facts.int_value(last.get('period'))
            day=facts.norm(first.get('day')).upper()
            label = f"{day} P{p1}" + (f'–P{p2}' if p2 != p1 else '')
            start=clock_slots.get((day,p1),{}).get('start_time')
            end=clock_slots.get((day,p2),{}).get('end_time')
            if start and end:
                label += f' [{start}–{end}]'
            label += f" ({facts.norm(first.get('group_id'))})"
            all_sessions.append((days.get(facts.norm(first.get('day')).upper(),99),p1,label))
        all_sessions.sort()
        text = '; '.join(item[2] for item in all_sessions[:12])
        if len(all_sessions) > 12:
            text += f'; … {len(all_sessions)-12} more sessions'
        lines.append(f'- **{name}:** {text}')
    return (f'**{course} · {title} — {len(by_teacher)} scheduled teachers and timings:**\n\n'
            + '\n'.join(lines) + '\n\nTimes are sourced from the selected timetable; '
            'lab blocks are listed as one teaching session.')


def _student_consecutive(question, rows, data):
    q = facts.normalized(question)
    if not (re.search(r'\b(sections?|students?|batches|groups?)\b', q)
            and re.search(r'\b(consecutive|back.to.back|continuous|without a free period)\b', q)):
        return None
    threshold = 3
    word_numbers = {'two':2,'three':3,'four':4,'five':5,'six':6}
    m = re.search(r'\b(?:more than|over|exceeding|greater than)\s+(\d+|two|three|four|five|six)\b', q)
    if m:
        threshold = int(m.group(1)) if m.group(1).isdigit() else word_numbers[m.group(1)]
    groups = data.get('Student_Groups', [])
    parent_ids = sorted({facts.norm(g.get('parent_group_id')) or facts.norm(g.get('group_id'))
                         for g in groups if facts.norm(g.get('group_id'))})
    if not parent_ids:
        parent_ids = sorted({facts.norm(r.get('group_id')).split('__')[0] for r in rows
                             if facts.norm(r.get('group_id'))})
    catalog = {facts.norm(g.get('group_id')):g for g in groups}
    results = []
    for group in parent_ids:
        occupied = defaultdict(set)
        for row in rows:
            if facts._belongs(row, group, catalog):
                occupied[facts.norm(row.get('day')).upper()].add(facts.int_value(row.get('period')))
        for day, periods in occupied.items():
            streak = []
            for period in sorted(periods):
                if streak and period == streak[-1] + 1:
                    streak.append(period)
                else:
                    if len(streak) > threshold:
                        results.append((group,day,streak[0],streak[-1],len(streak)))
                    streak = [period]
            if len(streak) > threshold:
                results.append((group,day,streak[0],streak[-1],len(streak)))
    results.sort(key=lambda r:(-r[4],r[0],r[1],r[2]))
    if not results:
        return f'No section has more than {threshold} consecutive occupied periods in the selected timetable.'
    lines = [f'- **{group}**, {day} P{start}–P{end}: **{length} consecutive periods**'
             for group,day,start,end,length in results[:20]]
    if len(results)>20:
        lines.append(f'- … and {len(results)-20} more runs')
    return (f'**Student sections with more than {threshold} consecutive occupied periods** '
            f'({len(results)} streaks):\n\n' + '\n'.join(lines)
            + '\n\nThis is based on section/batch occupancy, not individual student IDs.')


def _teacher_section_room_free(question, rows, data):
    q = facts.normalized(question)
    if not (re.search(r'\b(common|together|same|mutual)\b',q)
            and re.search(r'\b(free|available|vacant|unallocated)\b',q)
            and re.search(r'\b(classrooms?|rooms?|laborator(?:y|ies)|labs?)\b',q)):
        return None
    teacher_names = _faculty_names(data, rows)
    teachers = facts._named_teachers_in_question(question,teacher_names)
    groups = _groups(data,rows)
    group = facts._group_match(question,groups,rows)
    if not teachers or not group:
        return 'Please specify a teacher and student section for a joint free-slot search.'
    if len(teachers) != 1:
        return 'Please specify exactly one teacher alongside the student section for this search.'
    teacher = teachers[0]
    day_request = facts.parse_day(question)
    faculty = {facts.norm(f.get('faculty_id')):f for f in data.get('Faculty',[])}
    group_catalog={facts.norm(g.get('group_id')):g for g in data.get('Student_Groups',[])}
    used_rooms={(facts.norm(r.get('day')).upper(),facts.int_value(r.get('period')),
                 facts.norm(r.get('room_id'))) for r in rows}
    room_kind='LAB' if re.search(r'\b(lab|laboratory)\b',q) else 'CLASSROOM'
    room_inventory=[]
    for room in data.get('Rooms', []):
        rid=facts.norm(room.get('room_id'))
        kind=facts.norm(room.get('room_type')).upper()
        if rid and (room_kind=='LAB' and 'LAB' in kind or room_kind=='CLASSROOM'
                    and ('CLASS' in kind or 'LECTURE' in kind)):
            room_inventory.append(rid)
    if not room_inventory:
        return f'No {"laboratory" if room_kind=="LAB" else "classroom"} inventory is available to verify the request.'
    choices=[]
    for slot in data.get('Time_Slots',[]):
        day=facts.norm(slot.get('day')).upper(); period=facts.int_value(slot.get('period'))
        if day_request and day != day_request:
            continue
        if facts.norm(slot.get('academic_classes_allowed')).lower() not in ('yes','true','y','1'):
            continue
        statement,status=facts._teacher_slot_status(teacher,day,period,rows,data,faculty)
        if status!='AVAILABLE':
            continue
        if any(facts.norm(r.get('day')).upper()==day
               and facts.int_value(r.get('period'))==period
               and facts._belongs(r,group,group_catalog) for r in rows):
            continue
        rooms=[r for r in room_inventory if (day,period,r) not in used_rooms]
        if rooms:
            choices.append((day,period,rooms))
    choices.sort(key=lambda x:(['MON','TUE','WED','THU','FRI','SAT','SUN'].index(x[0])
                               if x[0] in ('MON','TUE','WED','THU','FRI','SAT','SUN') else 9,x[1]))
    if not choices:
        return f'No confirmed joint classroom/teacher/section free period was found for **{teacher}** and **{group}**. Faculty availability or student enrollment data may be incomplete.'
    lines=[f'- **{day} P{period}** — available {room_kind.lower()} example: **{rooms[0]}**'
           for day,period,rooms in choices[:16]]
    if len(choices)>16:
        lines.append(f'- … {len(choices)-16} more combinations')
    return (f'**Common free periods for {teacher} and {group}** (with an unallocated '
            f'{room_kind.lower()}):\n\n' + '\n'.join(lines)
            + '\n\nThese are free-period candidates, **not independently validated class moves**. '
            'Capacity, session duration and all institutional policies still require validation.')


def _uneven_faculty_workloads(question, rows, data):
    """Rank weekday imbalance instead of mistakenly selecting highest total load."""
    q = facts.normalized(question)
    if not (re.search(r'\b(teachers?|faculty|professors?|instructors?)\b', q)
            and re.search(r'\b(uneven|imbalanc\w*|least balanced|most variable)\b', q)
            and re.search(r'\b(workload|teaching|weekdays?|days?)\b', q)):
        return None
    weekdays = ('MON', 'TUE', 'WED', 'THU', 'FRI')
    by_faculty = defaultdict(lambda: defaultdict(int))
    for row in rows:
        faculty = facts.norm(row.get('faculty_name'))
        day = facts.norm(row.get('day')).upper()
        if faculty and day in weekdays:
            by_faculty[faculty][day] += 1  # Each CSV row represents one occupied period.
    if not by_faculty:
        return 'No teacher workload data is available in the selected timetable.'
    scores = []
    for name, days in by_faculty.items():
        loads = [days.get(day, 0) for day in weekdays]
        scores.append((max(loads)-min(loads), max(loads), sum(loads), name, loads))
    scores.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    chosen = scores[:5]
    labels = 'Mon, Tue, Wed, Thu, Fri'
    lines = [f'- **{name}** — weekday spread **{spread} periods**; '
             f'daily loads **{", ".join(map(str, loads))}** (Mon–Fri), '
             f'weekly total **{total} periods**.'
             for spread, _, total, name, loads in chosen]
    return ('**Teachers with the most uneven weekday teaching loads** '
            '(measured by maximum minus minimum daily periods):\n\n'
            + '\n'.join(lines)
            + '\n\nTeaching-period counts are calculated from the selected timetable. '
              'This ranks imbalance, not highest total workload.')


def answer_high_confidence(question, rows, data):
    """Return None when the question should use normal conversational routing."""
    for handler in (_uneven_faculty_workloads, _student_consecutive, _teacher_section_room_free, _course_times):
        result=handler(question,rows,data)
        if result is not None:
            return result
    return None


def answer_requested_placement(question, rows, data, *, include_mentoring=True,
                               extra_labs=0):
    """Check an actual proposed move, not the class's present Wednesday slot.

    This is deliberately read-only. A course may contain several distinct
    sessions; we ask the coordinator to identify one rather than treating an
    arbitrary course/section match as a feasible move.
    """
    q=facts.normalized(question)
    if not (re.search(r'\b(can|could|would|is it possible|is it feasible)\b',q)
            and re.search(r'\b(schedul\w*|placed?|move|shift|reschedul\w*)\b',q)
            and re.search(r'\b(period|slot|p\d+|at \d+)\b',q)):
        return None
    from timetable_chat_moves import matching_sessions
    course,ambiguous=_valid_course(question,rows,data)
    if ambiguous:
        return 'Which course code should I check: '+', '.join(ambiguous)+'? No timetable change has been made.'
    if not course:
        return 'Please give a course ID and section for a placement feasibility check.'
    group=facts._group_match(question,_groups(data,rows),rows)
    if not group:
        return f'Which section of **{course}** should I check? No timetable change has been made.'
    day=facts.parse_day(question)
    period=facts.parse_period(question) or facts._ordinal_period(question)
    if not day or period is None:
        return 'Specify an exact weekday and period to check. No timetable change has been made.'
    slots=[s for s in data.get('Time_Slots',[])
           if facts.norm(s.get('day')).upper()==day
           and facts.int_value(s.get('period'))==period]
    if not slots:
        return f'No defined {day} Period {period} exists in this workbook. No change has been made.'
    target=facts.norm(slots[0].get('slot_id'))
    # Select current class by course, section and optional kind, but do not
    # treat the requested future day/period as its existing start.
    kind='PRACTICAL' if re.search(r'\b(?:practical|lab)\b',q) else 'THEORY' if re.search(r'\btheory\b',q) else None
    matched=[]
    sessions=defaultdict(list)
    for row in rows:
        if (facts.norm(row.get('course_id'))==course and facts._belongs(
                row,group,{facts.norm(g.get('group_id')):g for g in data.get('Student_Groups',[])})
                and (not kind or facts.norm(row.get('kind')).upper()==kind)):
            sessions[facts.norm(row.get('session_id'))].append(row)
    matched=list(sessions)
    if not matched:
        return f'No scheduled class matches **{course} / {group}**. No timetable change has been made.'
    if len(matched)>1:
        details=[]
        for sid in sorted(matched)[:10]:
            block=sessions[sid]
            first=min(block,key=lambda r:facts.int_value(r.get('period')))
            details.append(f'- `{sid}` — {first.get("kind")} · {first.get("day")} P{first.get("period")} · {first.get("faculty_name")}')
        return (f'**{len(matched)} scheduled sessions match {course} / {group}.** '
                f'Which existing session should be checked for **{day} P{period}**?\n\n'
                +'\n'.join(details)+'\n\nNo timetable change has been made.')
    sid=matched[0]
    block=sessions[sid]
    if any(facts.norm(r.get('day')).upper()==day and facts.int_value(r.get('period'))==period
           for r in block):
        return f'**{course} / {group}** already occupies **{day} P{period}**. No move is needed.'
    from timetable_chat_moves import prepare_for_session
    reply,state=prepare_for_session(sid,rows,data,include_mentoring=include_mentoring,
                                    extra_labs=extra_labs,requested_slot=target,
                                    max_options=max(12,len(data.get('Time_Slots',[]))))
    proposal=state.get('proposal') or {}
    verified=next((option for option in proposal.get('options',[])
                   if option.get('slot_id')==target),None)
    if verified:
        return (f'**Yes — {course} / {group}** has an independently validated '
                f'alternative starting at **{verified["slot"]}** in **{verified["room_id"]}**. '
                'The complete class duration and other sessions were checked. '
                'Nothing was moved; coordinator approval is still required.')
    reasons=proposal.get('requested_reasons',[]) or proposal.get('reasons',[])
    reason='; '.join(str(x) for x in reasons[:4]) if reasons else 'The requested class placement did not pass the full independent checks.'
    return (f'**No validated move for {course} / {group} to {day} P{period}.** '
            f'Verified diagnostics: {reason} No timetable change has been made.')
