"""Read-only natural-language planner for Timetable Assistant.

Architecture: question -> intent/slots (Python or local Qwen JSON) -> trusted
entities -> registered read-only data tool -> deterministic answer.  No model
output is used as a timetable fact or as authorization to edit a schedule.

The router is intentionally independent of Streamlit and database connections.
"""
from __future__ import annotations

import json
import re
import urllib.request
from collections import defaultdict

import timetable_assistant as facts

OLLAMA_URL = 'http://127.0.0.1:11434/api/generate'
INTENTS = frozenset({
    'teacher_availability', 'teacher_sections', 'teacher_courses',
    'course_teachers', 'course_sections', 'section_courses',
    'schedule', 'room_availability', 'common_availability',
    'session_count', 'policy', 'validation', 'move_guidance',
    'summary', 'help', 'teacher_workload', 'section_availability',
    'room_utilization', 'teaching_comparison', 'explain_constraints', 'dataset_query', 'unknown',
})
DAY_WORDS = facts.DAY_NAMES
DAY_TO_TEXT = facts.DAY_NAMES
SHORT_FOLLOWUP = re.compile(
    r'^(?:and\s+)?(?:what about|how about|and on|and at|on|at|for|same for|what if|then|is (?:he|she|that teacher)|when (?:is|are) (?:he|she))\b',
    re.IGNORECASE,
)


# "Entire week" is a *scope selection* for a pending schedule, not a new
# request for another teacher. Do not treat every occurrence of "week" as a
# schedule query: e.g. "highest workload this week" is a different operation.
_FULL_WEEK_SCOPE = re.compile(
    r'\b(?:(?:entire|full|whole|complete|all)\s+(?:working\s+)?week|'
    r'(?:entire|full|whole|complete)\s+(?:weekly\s+)?(?:timetable|schedule)|'
    r'weekly\s+(?:timetable|schedule)|every\s+day|all\s+days)\b|'
    r'^(?:for\s+)?(?:the\s+)?(?:week|weekly)(?:\s+please)?$', re.I)


def _full_week_request(question):
    return bool(_FULL_WEEK_SCOPE.search(question or ''))


def _low(text):
    return facts.normalized(text)


def _codes(data, rows):
    catalog = {facts.norm(r.get('course_id')): r for r in data.get('Courses', [])}
    for row in rows:
        code = facts.norm(row.get('course_id'))
        if code and code not in catalog:
            catalog[code] = row
    return catalog


def _teachers(data, rows):
    names = {facts.norm(r.get('faculty_name')) for r in rows}
    names.update(facts.norm(r.get('faculty_name')) for r in data.get('Faculty', []))
    return names - {''}


def _groups(data, rows):
    names = {facts.norm(r.get('group_id')) for r in rows}
    names.update(facts.norm(r.get('group_id')) for r in data.get('Student_Groups', []))
    return names - {''}


def _local_intent(q):
    """Broad, fast offline baseline; Ollama optionally improves unusual phrasing."""
    s = _low(q)
    if re.search(r'\b(move|shift|reschedul|rearrange|relocate|swap|transfer|change the time|different time|alternative slot|another slot)\b', s):
        return 'move_guidance'
    if _full_week_request(s):
        return 'schedule'
    if re.search(r'\b(hard constraints?|soft constraints?|polic(?:y|ies)|rule|restricted|discouraged|thursday afternoon|open elective|reserved)\b', s):
        return 'policy'
    if re.search(r'\b(validat|validity|violations?|clash|conflicts?|verified timetable|feasible)\b', s):
        return 'validation'
    if re.search(r'\b(what can you do|help me use|how do you work|what questions|your capabilities|explain your capabilities)\b', s):
        return 'help'
    if re.search(r'\b(compare|better|difference between)\b', s):
        return 'unknown'  # Different timetables need two explicitly selected sources.
    if re.search(r'\b(common|both|mutual|together|overlap|same time)\b', s) and re.search(r'\b(free|available|meet|time|slot)\b',s):
        return 'common_availability'
    if re.search(r'\b(laborator(?:y|ies)|labs?|classrooms?|rooms?)\b', s) and re.search(r'\b(free|vacant|open|empty|available|unused|find|which)\b', s):
        return 'room_availability'
    if re.search(r'\bwhen\s+(does|did|will)\b.*\b(teach|lecture|have classes)\b',s):
        return 'schedule'
    if re.search(r'\b(when|free|available|vacant|busy|occupied|unavailable|meet|openings|open slots|empty slots|spare time|spare periods|catch up|catchup|talk to|talk with|speak to|speak with|appointment|meeting|attend a meeting|take a call)\b', s) and not re.search(r'\b(subjects?|courses?)\b',s):
        return 'teacher_availability'
    if re.search(r'\b(how many|count|total|number of)\b',s):
        return 'session_count'
    if re.search(r'\b(sections?|cohorts?|batches?|student groups?|classes)\b', s) and re.search(r'\b(teach|handle|responsib|assigned|faculty|instructor|take|lecture)\b',s):
        return 'teacher_sections'
    if re.search(r'\b(subjects?|courses?|modules?)\b',s) and re.search(
            r'\b(teach|teaches|teaching|taught|handle|handles|handled|instruct|instructs|instructed|assigned|faculty|delivered|conducted)\b',s):
        if re.search(r'\b(which|what|list|all|does|are|give|tell|name)\b',s):
            return 'teacher_courses'
    if re.search(r'\b(who|faculty|teachers?|instructors?|staff|professors?|handles?|responsible|takes?|deliver)\b',s) and re.search(r'\b(teach|teaches|handle|handles|instruct|deliver|responsible|responsibility|course|subject|module|staff|faculty)\b',s):
        return 'course_teachers'
    if re.search(r'\b(which|what|list|all|offer|studies|takes)\b',s) and re.search(r'\b(courses?|subjects?|modules?)\b',s):
        return 'section_courses'
    if re.search(r'\b(timetable|schedule|classes|lectures|periods|teach on|teaching on|what is happening|what happens)\b',s):
        return 'schedule'
    if re.search(r'\b(summary|overview|statistics|stats)\b',s):
        return 'summary'
    return 'unknown'


def _call_qwen_intent(question, *, teacher_names, course_codes, recent_question='',
                      model='openai/gpt-oss-120b', timeout_seconds=13):
    """Compatibility entry point: Groq planning, never a model-generated fact.

    Do not transmit the workbook's faculty list or course inventory to a cloud
    service. Exact names and identifiers are resolved locally by Python.
    """
    from timetable_cloud_ai import classify_question
    return classify_question(question, recent_question=recent_question, model=model)



def _slot_reply(question, teachers, groups, catalog):
    """A short answer supplying an entity/period, not a new request.

    Ground matching in the workbook/CSV. An isolated teacher name or "for
    Period 2" normally completes the assistant's previous clarification.
    Explicit questions are *not* treated as slot replies, so switching topics
    stays possible without carrying the previous intent accidentally.
    """
    if _local_intent(question) != 'unknown':
        return False
    q = _low(question).strip(' .?!')
    if any(q == _low(x) or q == 'for ' + _low(x) for x in teachers | groups | set(catalog)):
        return True
    # "Manvi Bareja maam" is still an entity-only reply to the pending
    # request. Honorifics must not force the assistant to forget "full week".
    for name in teachers:
        if re.fullmatch(r'(?:for )?(?:maam |mam |madam |sir )?'
                        + re.escape(_low(name)) + r'(?: maam| mam| madam| sir)?', q):
            return True
    if re.fullmatch(r'(?:on |for |and on )?(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)', q):
        return True
    if re.fullmatch(r'(?:for |at |and |the )?(?:period|p)\s*[-#: ]*\d{1,2}(?:\s*(?:please|only))?', q):
        return True
    if re.fullmatch(r'(?:for |at |and |the )?\d{1,2}(?:st|nd|rd|th)\s+period', q):
        return True
    # A clarification like "all sections" is a scope choice, not a request
    # to forget the teacher or the original teacher-course question.
    if re.fullmatch(r'(?:(?:for|across|in)\s+)?(?:all|every)\s+(?:sections?|classes|groups|batches)(?:\s+please)?', q):
        return True
    return False

def _resolve_context(conversation, data, rows):
    """Resolve last user request using SOURCE text; never infer from model prose."""
    previous = [entry.get('content','') for entry in (conversation or [])
                if isinstance(entry,dict) and entry.get('role') == 'user'][-10:]
    names, catalog, groups = _teachers(data,rows), _codes(data,rows), _groups(data,rows)
    ctx = {'teacher':None,'course':None,'group':None,'day':None,'period':None,'clock':None,
           'intent':None,'question':'','topic_question':'','week_scope':False}
    from timetable_spelling import normalize_question
    for question in previous:
        spelling=normalize_question(question,data,rows)
        question=spelling.question if not spelling.clarification else question
        teacher,_=facts.match_teacher(question,names)
        course,amb=facts.match_course_reference(question,catalog,rows)
        group=facts._group_match(question,{g:{} for g in groups},rows)
        day=facts.parse_day(question)
        explicit_new_topic = not (SHORT_FOLLOWUP.search(question.strip()) or
                                  _slot_reply(question, names, groups, catalog))
        new_intent = _local_intent(question)
        if explicit_new_topic and (teacher or course or group or new_intent in
                                   ('policy','validation','summary','move_guidance','help','room_availability')):
            ctx = {'teacher':None,'course':None,'group':None,'day':None,'period':None,'clock':None,
           'intent':None,'question':'','topic_question':'','week_scope':False}
        if teacher: ctx['teacher']=teacher
        if course and not amb: ctx['course']=course
        if group: ctx['group']=group
        if _full_week_request(question):
            ctx['week_scope']=True
            ctx['day']=None
            ctx['period']=None
            ctx['clock']=None
        elif day:
            ctx['day']=day
            ctx['week_scope']=False
        period=facts.parse_period(question) or facts._ordinal_period(question)
        clock=facts.parse_clock_minutes(question)
        # New explicit time/period replaces the old time selection. Otherwise
        # a follow-up such as 'and at 2:30?' could accidentally inherit a
        # previously selected Period 4 and report a false contradiction.
        if period is not None:
            ctx['period']=period
            if clock is None: ctx['clock']=None
        if clock is not None:
            ctx['clock']=clock
            if period is None: ctx['period']=None
        intent=_local_intent(question)
        if intent != 'unknown':
            ctx['intent']=intent
            ctx['topic_question']=question
        ctx['question']=question
    return ctx


def _selected_entities(question, rows, data, *, conversation=(), llm_result=None):
    teachers, catalog, groups = _teachers(data,rows), _codes(data,rows), _groups(data,rows)
    teacher,_=facts.match_teacher(question,teachers)
    course,ambiguous=facts.match_course_reference(question,catalog,rows)
    group=facts._group_match(question,{g:{} for g in groups},rows)
    day=facts.parse_day(question)
    period=facts.parse_period(question) or facts._ordinal_period(question)
    clock=facts.parse_clock_minutes(question)
    if clock is not None and day and clock < 8*60:
        # A bare "2:30" means afternoon ONLY when it is the unique teaching
        # slot interpretation; explicit 2:30 AM is never reinterpreted.
        time_pattern = re.search(r'(?<!\w)\d{1,2}[:.]\d{2}\s*(a\.?m\.?|p\.?m\.?)?',question,re.I)
        if time_pattern and not time_pattern.group(1) and \
                facts._slot_for_clock_time(data,day,clock) is None and \
                facts._slot_for_clock_time(data,day,clock+720) is not None:
            clock += 720
    ctx=_resolve_context(conversation,data,rows)
    pronoun = bool(re.search(r'\b(he|she|his|her|they|their|that teacher|same teacher|same course|it|those|them)\b',question,re.I))
    follow = (bool(SHORT_FOLLOWUP.search(question.strip())) or
              _slot_reply(question, teachers, groups, catalog) or
              (_full_week_request(question) and ctx['intent'] == 'schedule') or
              (pronoun and len(question.split())<=14) or
              (len(question.split()) <= 6 and day and not teacher and not course and not group))
    # A bare teacher name can complete "full week" / "entire week".
    # But a new question about another topic must not inherit the week scope.
    full_week = (_full_week_request(question) or
                 (follow and ctx.get('week_scope') and
                  _slot_reply(question, teachers, groups, catalog)) or
                 # Repeating a same-teacher timetable request after asking for
                 # the whole week should not start another day-clarification.
                 (ctx.get('week_scope') and _local_intent(question) == 'schedule'
                  and bool(teacher) and teacher == ctx['teacher']
                  and not day and not course and not group))
    if follow:
        teacher = teacher or ctx['teacher']
        course = course or ctx['course']
        group = group or ctx['group']
        # A new question type about the same person must not silently inherit
        # the previous question's day. 'Which sections does she teach?' means
        # all sections, not only those taught on the day last discussed.
        explicit_intent = _local_intent(question)
        if (not day and not full_week and explicit_intent in
                ('unknown', 'teacher_availability', 'common_availability',
                 'schedule', 'room_availability', 'session_count')):
            day = ctx['day']
        if full_week:
            day = None
            period = None
            clock = None
        # A weekday-only continuation of a single-period question retains its
        # previously selected period, but a full-day query does not invent one.
        # Retain the exact time when the coordinator answers a clarification
        # with only a teacher/day. Do not inherit a time when the new message
        # explicitly selects a different period.
        if (clock is None and period is None and ctx['clock'] is not None
                and explicit_intent in ('unknown','teacher_availability','common_availability')
                and (_slot_reply(question,teachers,groups,catalog)
                     or (day and bool(SHORT_FOLLOWUP.search(question.strip()))))):
            clock=ctx['clock']
        if (period is None and clock is None and ctx['period'] is not None
                and explicit_intent=='unknown' and
                (_slot_reply(question,teachers,groups,catalog)
                 or (day and bool(SHORT_FOLLOWUP.search(question.strip()))))):
            period=ctx['period']
    # An LLM may propose a known entity only if already present literally in
    # the user question or an established prior user turn; it cannot fabricate.
    if llm_result:
        source = _low(question + ' ' + (ctx['question'] if follow else ''))
        if not teacher:
            candidate=str(llm_result.get('teacher') or '')
            if candidate in teachers and _low(candidate) in source:
                teacher=candidate
        if not course:
            candidate=str(llm_result.get('course') or '')
            if candidate in catalog and _low(candidate) in source:
                course=candidate
        if not group:
            candidate=str(llm_result.get('section') or '')
            if candidate in groups and _low(candidate) in source:
                group=candidate
    return dict(teacher=teacher,course=course,ambiguous=ambiguous,group=group,day=day,
                period=period,clock=clock,ctx=ctx,follow=follow,full_week=bool(full_week))


def _is_two_period(question, llm):
    low = _low(question)
    return bool(re.search(r'\b(two|2)\b.*\b(hours?|periods?|slots?)\b|\b(back to back|consecutive|continuous)\b',low)
                or (llm and llm.get('two_periods') is True))


def _room_kind(question, llm):
    low = _low(question)
    if re.search(r'\b(laboratories|laboratory|labs|lab)\b',low): return 'Lab'
    if re.search(r'\b(classrooms|classroom)\b',low): return 'Classroom'
    if re.search(r'\b(rooms|room)\b',low): return 'All'
    return str(llm.get('room_kind')) if llm and llm.get('room_kind') in ('Lab','Classroom') else 'Classroom'


def _capacity(question):
    return facts._room_capacity_requested(question)


def _course_sections(course,rows,groups):
    found=set()
    for r in rows:
        if facts.norm(r.get('course_id')) == course:
            g=facts.norm(r.get('group_id'))
            if g:
                record=groups.get(g,{})
                found.add(facts.norm(record.get('parent_group_id')) or g.split('__')[0])
    if not found: return f'No sections found for **{course}** in this selected timetable.'
    return f'**{course} is scheduled for {len(found)} sections:**\n\n'+'\n'.join('- '+x for x in sorted(found))


def _read_only_dispatch(intent, ent, question, rows, data, report, llm_result):
    """Registered trusted read-only tools. Returns None if clarification needed."""
    teacher,course,group,day,period,clock=(ent[k] for k in ('teacher','course','group','day','period','clock'))
    code_groups={facts.norm(g.get('group_id')):g for g in data.get('Student_Groups',[])}
    course_catalog=_codes(data,rows)
    faculty={facts.norm(f.get('faculty_id')):f for f in data.get('Faculty',[])}
    if ent['ambiguous']:
        return 'That subject name matches multiple courses: '+', '.join(ent['ambiguous'])+'. Which code do you mean?'
    if intent == 'teacher_sections':
        if not teacher: return 'Which teacher’s sections should I check?'
        return facts._teacher_section_directory(teacher,rows,code_groups,day=day)
    if intent == 'teacher_courses':
        if not teacher and group:
            return _read_only_dispatch('section_courses', ent, question, rows, data, report, llm_result)
        if not teacher: return 'Which teacher’s courses should I check?'
        found={}
        for r in rows:
            if facts.norm(r.get('faculty_name'))==teacher and (not day or facts.norm(r.get('day'))==day):
                found[facts.norm(r.get('course_id'))]=facts.norm(r.get('course_name'))
        if not found: return f'No courses are scheduled for **{teacher}**'+(f' on {DAY_WORDS[day]}' if day else '')+'.'
        return f'**{teacher} teaches {len(found)} {"course" if len(found) == 1 else "courses"}:**\n\n'+'\n'.join(f'- **{k}** — {v}' for k,v in sorted(found.items()))
    if intent == 'course_teachers':
        if not course: return 'Which course or subject should I check?'
        filtered=[r for r in rows if facts.norm(r.get('course_id'))==course
                  and (not group or facts._belongs(r,group,code_groups))]
        include_sections=bool(re.search(r'\b(section|sections|batches|groups)\b',question,re.I))
        info=facts._course_teaching_assignments(course,filtered,code_groups,include_sections=include_sections)
        if not info: return f'No scheduled teachers found for **{course}** in this timetable.'
        count=len({facts.norm(r.get('faculty_name')) for r in filtered})
        title=facts.norm(course_catalog.get(course,{}).get('course_name'))
        return f'**{course} · {title} — {count} scheduled teacher{"s" if count != 1 else ""}:**\n\n'+info
    if intent == 'course_sections':
        if not course: return 'Which course should I check?'
        return _course_sections(course,rows,code_groups)
    if intent == 'section_courses':
        if not group: return 'Which section should I check?'
        found=defaultdict(set)
        for r in rows:
            if facts._belongs(r,group,code_groups):
                found[(facts.norm(r.get('course_id')),facts.norm(r.get('course_name')))].add(facts.norm(r.get('faculty_name')))
        if not found: return f'No scheduled courses found for **{group}**.'
        return f'**{group} — {len(found)} courses:**\n\n'+'\n'.join(f'- **{c} · {title}** — {", ".join(sorted(facts._faculty_display_name(n) for n in names))}' for (c,title),names in sorted(found.items()))
    if intent == 'teacher_availability':
        if not teacher: return 'Which teacher should I check?'
        if not day: return f'Which weekday should I check for **{teacher}**?'
        label=DAY_WORDS[day]
        # Multi-day requests must preserve exact user slots, rather than silently using one.
        segments=facts._mentioned_day_segments(question)
        if len(segments)>1: return facts._teacher_multiple_slot_status(question,teacher,segments,rows,data,faculty)
        if clock is not None:
            slot=facts._slot_for_clock_time(data,day,clock)
            if not slot: return f'That time is outside the defined teaching periods on {label}.'
            resolved=facts.int_value(slot.get('period'))
            if period is not None and period!=resolved:
                return f'**Period {period}** does not match the requested time, which falls in **Period {resolved}** on {label}. Which one should I use?'
            period=resolved
        if period is not None:
            statement,status=facts._teacher_slot_status(teacher,day,period,rows,data,faculty,clock_minutes=clock)
            if re.search(r'\b(busy|occupied|teaching)\b',question,re.I) and re.search(r'\b(is|was|are|does|has)\b',question,re.I):
                if status=='BUSY': statement=statement.replace('**No — '+teacher+' is teaching**','**Yes — '+teacher+' is busy teaching**')
                if status=='AVAILABLE': statement=statement.replace('**Yes — '+teacher+' is free**','**No — '+teacher+' is not busy**')
            return statement
        return facts._teacher_day_status(teacher,day,rows,data,faculty,meeting=bool(re.search(r'\b(meet|appointment|talk to)\b',question,re.I)))
    if intent == 'common_availability':
        names=facts._named_teachers_in_question(question,_teachers(data,rows))
        if len(names)!=2: return 'Please name two teachers to check their common free periods.'
        if not day: return 'Which day should I check for both teachers?'
        return facts._common_free_periods(names,day,rows,data,faculty)
    if intent == 'room_availability':
        if not day: return 'Which weekday should I check for available rooms?'
        # A period-only follow-up must preserve the original room filter.
        # Otherwise 'free rooms Monday' -> 'for period 2' silently narrows to
        # classrooms because the second message contains no word 'rooms'.
        room_question = question
        if ent.get('follow') and not re.search(
                r'\b(?:rooms?|labs?|laborator(?:y|ies)|classrooms?)\b', question, re.I):
            room_question = (ent['ctx'].get('topic_question') or '') + ' ' + question
        kind=_room_kind(room_question,llm_result)
        seats=_capacity(room_question)
        rooms={facts.norm(r.get('room_id')):r for r in data.get('Rooms',[])}
        if clock is not None:
            slot=facts._slot_for_clock_time(data,day,clock)
            if slot is None:return 'That time is not within a timetable period.'
            resolved=facts.int_value(slot.get('period'))
            if period is not None and period!=resolved:
                return f'The time is Period {resolved}, not Period {period}. Which do you want?'
            period=resolved
        if _is_two_period(room_question,llm_result):
            return facts._free_rooms_for_consecutive_periods(day,rows,data,rooms,kind=kind,seats=seats,start_period=period)
        if period is None:
            # A generic day-level classroom query can list dozens of names in
            # each of eight periods. Show the useful first-five preview and
            # daily totals, while allowing an explicit full listing or a
            # period-specific follow-up. Preserve the full-list behaviour for
            # ordinary 'rooms' queries used by existing coordinator reports.
            explicitly_full = bool(re.search(
                r'\b(?:list all|show all|every room|every classroom|complete list|full list|all names)\b',
                question, re.I))
            concise = bool(re.search(r'\b(?:brief|short|summary|sample|few|top five|first five)\b',
                                     question, re.I))
            show_all = explicitly_full or (kind != 'Classroom' and not concise)
            return facts._free_rooms_by_period(day, rows, data, rooms, kind=kind,
                                               seats=seats, show_all=show_all)
        used={facts.norm(r.get('room_id')) for r in rows if facts.norm(r.get('day'))==day and facts.int_value(r.get('period'))==period}
        free=sorted(k for k,v in rooms.items() if (kind=='All' or facts.norm(v.get('room_type')).lower()==kind.lower())
                    and facts.int_value(v.get('capacity'))>=seats and k not in used)
        room_label = ('rooms' if kind.upper() == 'ALL' else 'laboratories' if kind.upper() == 'LAB' else 'classrooms')
        capacity_label = f' with at least {seats} seats' if seats else ''
        return f'**{len(free)} {room_label}{capacity_label} are unallocated on {DAY_WORDS[day]}, Period {period}:**\n\n'+(', '.join(free) if free else 'None found.')+'\n\nRoom-only check; complete placement requires validation.'
    if intent == 'schedule':
        if not (teacher or group or course): return 'Which teacher, section or course timetable should I show?'
        selected=[r for r in rows if (not teacher or facts.norm(r.get('faculty_name'))==teacher)
                  and (not course or facts.norm(r.get('course_id'))==course)
                  and (not group or facts._belongs(r,group,code_groups))
                  and (not day or facts.norm(r.get('day'))==day)]
        if period is not None:
            selected=facts._whole_blocks(selected,[r for r in selected if facts.int_value(r.get('period'))==period])
        # A teacher's timetable normally means their weekly timetable. Do
        # not make the coordinator supply "full week" unnecessarily. The
        # same full-week scope also works as a follow-up without Groq.
        teacher_weekly_default = (bool(teacher) and not group and not course
                                  and not day and period is None and clock is None
                                  and bool(re.search(r'\b(timetable|schedule)\b', question, re.I)))
        if not day and not (ent.get('full_week') or _full_week_request(question)
                            or teacher_weekly_default):
            return 'Which day should I show? You can also ask for the complete weekly timetable.'
        title=' · '.join(x for x in (teacher,group,course, DAY_WORDS.get(day,'')) if x)
        if not day and teacher and not course and not group:
            title += ' · Weekly timetable'
        return facts._display_matches(selected,title)
    if intent == 'session_count':
        selected=[r for r in rows if (not teacher or facts.norm(r.get('faculty_name'))==teacher)
                  and (not course or facts.norm(r.get('course_id'))==course)
                  and (not group or facts._belongs(r,group,code_groups))
                  and (not day or facts.norm(r.get('day'))==day)]
        if not (teacher or group or course): return facts._count_summary(rows)
        count=len(facts._merge_sessions(selected))
        return f'**{count} distinct teaching session(s)** occupy {len(selected)} periods in the selected timetable.'
    if intent == 'policy':
        if re.search(r'\b(thursday|soft|discourage|avoid)\b',question,re.I):
            return facts.response('Why is Thursday afternoon discouraged for scheduling classes?',rows,data,report)
        return 'Institutional rules come from the Excel policy sheets. Open **Results → Institution policies** for the exact applicable restrictions.'
    if intent == 'validation':
        return facts._validation_answer(report)
    if intent == 'summary':return facts._count_summary(rows)
    if intent == 'help':
        return ('**I can check the selected timetable for:** teachers and their sections, '
                'course allocations, teacher availability, two-teacher common free periods, '
                'section and faculty schedules, classroom/laboratory openings, '
                'session counts, policies and validation. You can also ask follow-up '
                'questions such as “What about Thursday?”\n\n'
                'For a class move, I can identify the relevant sessions and guide '
                'you to a validated proposal. I cannot directly approve, publish, '
                'or guarantee an arbitrary timetable change.')
    if intent == 'move_guidance':
        from timetable_agent import _grounded_move_guidance
        return _grounded_move_guidance(question,rows,data)
    return None


def answer_structured(question,rows,data,validation_report=None,*,conversation=(),
                      llm_enabled=False,model='openai/gpt-oss-120b'):
    """Return a trusted-tool answer or None to let the legacy parser try.

    The model's answer text is never rendered. The current timetable CSV and
    workbook remain the sole authority for class/room/teacher availability.
    """
    q=(question or '').strip()
    if not q:return None
    from timetable_spelling import normalize_question
    spelling=normalize_question(q,data,rows)
    if spelling.clarification:return spelling.clarification
    q=spelling.question
    ctx=_resolve_context(conversation,data,rows)
    llm=None
    # When enabled, the language model is genuinely consulted for semantic
    # routing, not merely a cosmetic alternative explanation. Python fallback
    # stays usable without a cloud key or network.
    if llm_enabled:
        llm=_call_qwen_intent(q,teacher_names=_teachers(data,rows),
                              course_codes=_codes(data,rows),
                              recent_question=ctx['question'],model=model)
    ent=_selected_entities(q,rows,data,conversation=conversation,llm_result=llm)
    intent=_local_intent(q)
    # Reuse the previous topic ONLY when this turn has no identifiable intent.
    # In V13 a short pronoun follow-up such as 'Which sections does she teach?'
    # was incorrectly forced back to the previous availability question.
    if ent['follow'] and intent == 'unknown' and ctx['intent']:
        intent = ctx['intent']
    # The model fills genuine intent gaps. Explicit Python-recognized
    # requests are not overridden by a fallible small language model.
    # This prevents a wrongly classified request from silently changing its
    # meaning (for example, availability interpreted as course teachers).
    if llm and llm['intent'] != 'unknown' and intent == 'unknown':
        intent=llm['intent']
    if intent == 'unknown':return None
    return _read_only_dispatch(intent,ent,q,rows,data,validation_report,llm)
