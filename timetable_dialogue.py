"""Verified conversational tool planner for Timetable Assistant.

Handles source-grounded multi-part questions and multi-turn move context.
The optional Groq LLM may classify unfamiliar wording; only verified Python
tools provide timetable facts. Any move needs independent validation and
explicit coordinator confirmation.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import re

import timetable_assistant as facts
import timetable_nl_router as router

# This is a tool registry, not a list of fixed question/answer pairs. Extending
# the assistant means supplying another verified operation, not memorising a
# user's sentence.
TOOL_NAMES = frozenset({
    'teacher_availability', 'teacher_sections', 'teacher_courses',
    'course_teachers', 'course_sections', 'section_courses', 'schedule',
    'room_availability', 'common_availability', 'session_count',
    'policy', 'validation', 'move_guidance', 'summary', 'help',
    'teacher_workload', 'section_availability', 'room_utilization',
    'teaching_comparison', 'explain_constraints', 'dataset_query',
})


def _question_intent(question):
    """Conservative offline routing for categories with genuine data tools.

    Unrecognized queries go to the optional structured LLM planner; the
    fallback is a targeted clarification rather than a fabricated answer.
    """
    q = facts.normalized(question)
    if re.search(r'\b(why|what happens|what if|how does|explain|can|could|is it allowed)\b', q) and re.search(
            r'\b(clashes?|collisions?|overlap|same room|same teacher|double book|two teachers|two classes|capacity|constraint)\b', q):
        return 'explain_constraints'
    # Explicit requests for *free rooms* belong to room availability, even
    # when they mention a minimum capacity or consecutive periods.
    if re.search(r'\b(rooms?|laborator(?:y|ies)|labs?|classrooms?)\b',q) and re.search(
            r'\b(free|available|vacant|open|find|can i use|can we use)\b',q):
        return None
    if re.search(r'\b(most|busiest|highest|least|fewest|total|how many|count|back to back|consecutive|workload|teaching load|teaching hours?|heaviest|lightest|load|teaching periods|gaps?)\b',q) and re.search(r'\b(teachers?|faculty|professors?|maam|madam|she|he|his|her|teach|classes|lectures|periods?|swati|manvi|preeti|nidhi|aarti)\b',q):
        return 'teacher_workload'
    if re.search(r'\b(which|what|how many|first|last|free|vacant|empty|spare|unallocated)\b',q) and re.search(r'\b(periods?|slots?|time|class|lectures?)\b',q) and re.search(r'\b(section|batch|students?|cohort|group|btechcse|mca|bca)\b',q):
        return 'section_availability'
    if re.search(r'\b(rooms?|labs?|laborator(?:y|ies)|classrooms?)\b',q) and re.search(r'\b(occupied|in use|booked)\b',q) and not re.search(r'\b(free|available|vacant)\b',q):
        return 'dataset_query'
    if re.search(r'\b(rooms?|labs?|laborator(?:y|ies)|classrooms?)\b',q) and re.search(r'\b(most|least|busiest|frequently|utilization|utilisation|unused|never|completely|occupied all day|entire day)\b',q):
        return 'room_utilization'
    if re.search(r'\b(which|who|list|how many|count|number|most|least)\b',q) and (
        re.search(r'\b(teachers?|faculty|sections?|courses?|subjects?|rooms?|sessions?|practicals?)\b',q)
        and re.search(r'\b(teach|work|teaching|classes|sessions|practical|theory|occur|scheduled|on monday|on tuesday|on wednesday|on thursday|on friday|most|least)\b',q)):
        return 'dataset_query'
    if re.search(r'\b(compare|versus|vs|difference|more classes|more teaching|busiest between)\b',q) and re.search(r'\b(teachers?|faculty|professors?|teach|loads?|workload)\b',q):
        return 'teaching_comparison'
    if re.search(r'\b(switch|move|shift|reschedule|swap|relocate|change|alternative slot|other slot)\b',q):
        return 'move_guidance'
    # 'any other teacher' and related negatives are modifiers on course_teachers.
    if re.search(r'\b(another|any other|other|else|apart from|besides|only one)\b',q) and re.search(r'\b(teachers?|faculty|instructors?|professor|who)\b',q):
        return 'course_teachers'
    return None


def _turn_context(conversation, rows, data):
    """Explicit request state reconstructed from chat turns, not past LLM facts.

    We don't trust a saved model answer as an entity unless it exactly matches
    ONE teacher in the current timetable's faculty registry.
    """
    ctx = router._resolve_context(conversation, data, rows)
    all_names = router._teachers(data, rows)
    last_assistant = next((str(x.get('content') or '') for x in reversed(conversation or [])
                           if isinstance(x, dict) and x.get('role') == 'assistant'), '')
    last_user = next((str(x.get('content') or '') for x in reversed(conversation or [])
                      if isinstance(x, dict) and x.get('role') == 'user'), '')
    mentioned = [name for name in all_names if facts.match_value(last_assistant, (name,))]
    if len(mentioned) == 1 and not ctx['teacher']:
        ctx['teacher'] = mentioned[0]
    ctx['last_assistant'] = last_assistant
    ctx['last_user'] = last_user
    previous = [str(x.get('content') or '') for x in (conversation or [])
                if isinstance(x, dict) and x.get('role') == 'user'][-8:]
    last_assistant_lower = last_assistant.lower()
    # A cancelled or superseded move cannot hijack a later availability query.
    if any(term in last_assistant_lower for term in ('move preview cancelled',
                                                     'class-move preview cancelled')):
        ctx['pending_move'] = ''
    else:
        ctx['pending_move'] = next((s for s in reversed(previous[-3:])
                                    if _question_intent(s) == 'move_guidance'
                                    or router._local_intent(s) == 'move_guidance'), '')
    return ctx


def _entities(question, rows, data, conversation):
    ent = router._selected_entities(question, rows, data, conversation=conversation)
    state = _turn_context(conversation, rows, data)
    q = facts.normalized(question)
    inherits = bool(ent.get('follow') or re.search(
        r'\b(other|another|else|besides|apart from|only one|just one|same|that|previous|it|she|he|her|him|their|them)\b',q))
    if inherits:
        for key, context_key in (('teacher','teacher'),('course','course'),('group','group')):
            if not ent[key]: ent[key] = state[context_key]
    # A previous answer that named exactly one faculty member may ground a
    # pronoun, but not a request that introduces a new explicit teacher.
    if not ent['teacher'] and re.search(r'\b(she|he|her|him|that teacher)\b', q):
        ent['teacher'] = state['teacher']
    ent['memory'] = state
    return ent


def _teacher_load(question, ent, rows, data):
    name, day = ent['teacher'], ent['day']
    q = facts.normalized(question)
    if not name and not re.search(r'\b(most|highest|busiest|least|fewest|heaviest|lightest)\b',q):
        return 'Which teacher should I check?'
    if re.search(r'\b(gaps?|breaks between classes)\b',q):
        loads=defaultdict(lambda: defaultdict(set))
        for r in rows:
            label=facts.norm(r.get('faculty_name'))
            if label and (not name or label==name):
                loads[label][facts.norm(r.get('day'))].add(facts.int_value(r.get('period')))
        gaps={person:sum((max(ps)-min(ps)+1-len(ps)) for ps in weekdays.values() if ps)
              for person,weekdays in loads.items()}
        if not gaps:return 'No scheduled faculty data is available to calculate gaps.'
        if name:return f'**{name} has {gaps.get(name,0)} internal timetable gap period(s)** between its first and last class of each teaching day.'
        high=max(gaps.values());names=sorted(k for k,v in gaps.items() if v==high)
        return f'**{", ".join(names)}** has the most internal timetable gaps: {high} period(s).'
    if not name:
        loads=Counter(facts.norm(r.get('faculty_name')) for r in rows if facts.norm(r.get('faculty_name')))
        if not loads:return 'No scheduled faculty periods are available.'
        reverse=not bool(re.search(r'\b(least|fewest|lightest)\b',q))
        target=max(loads.values()) if reverse else min(loads.values())
        people=sorted(n for n,v in loads.items() if v==target)
        return f'**{", ".join(people)}** {"has" if len(people)==1 else "have"} {target} scheduled teaching period(s) this week' + (' (including both periods of practicals).' if target!=1 else '.')
    selected=[r for r in rows if facts.norm(r.get('faculty_name'))==name and (not day or facts.norm(r.get('day'))==day)]
    days=Counter(facts.norm(r.get('day')) for r in rows if facts.norm(r.get('faculty_name'))==name)
    if not days:return f'No classes are scheduled for **{name}** in this timetable.'
    if re.search(r'\b(how many days|days per week|number of days)\b',q):
        return f'**{name} teaches on {len(days)} day(s)** this week: '+', '.join(facts.DAY_NAMES.get(d,d) for d in sorted(days))+'.'
    if re.search(r'\b(busiest|most loaded|heaviest|most classes)\b',q):
        maximum=max(days.values())
        winners=[facts.DAY_NAMES.get(d,d) for d,n in days.items() if n==maximum]
        return f'**{name} is busiest on {", ".join(sorted(winners))}**, with {maximum} teaching period(s).'
    if re.search(r'\b(back to back|consecutive|continuous|straight)\b',q):
        by_day=defaultdict(set)
        for r in selected: by_day[facts.norm(r.get('day'))].add(facts.int_value(r.get('period')))
        runs=[]
        for d, ps in by_day.items():
            ordered=sorted(ps); start=last=None
            for p in ordered+[None]:
                if p is not None and last is not None and p==last+1: last=p;continue
                if start is not None and last is not None and last>start:
                    runs.append(f'{facts.DAY_NAMES.get(d,d)} Periods {start}–{last}')
                start=last=p
        return (f'**Yes.** {name} has consecutive teaching periods: '+', '.join(runs)+'.' if runs else f'**No.** {name} has no consecutive teaching periods'+(f' on {facts.DAY_NAMES.get(day,day)}' if day else '')+'.')
    records=facts._merge_sessions(selected)
    where=f' on {facts.DAY_NAMES.get(day,day)}' if day else ' this week'
    return f'**{name} teaches {len(selected)} period(s) in {len(records)} distinct session(s){where}.**' + (' Practical blocks count as one session but occupy two periods.' if any(facts.norm(r.get('kind')).upper()=='PRACTICAL' for r in selected) else '')


def _section_free(question, ent, rows, data):
    group, day=ent['group'],ent['day']
    if not group:return 'Which student section or batch should I check?'
    if not day:return f'Which day should I check for **{group}**?'
    catalog={facts.norm(g.get('group_id')):g for g in data.get('Student_Groups',[])}
    slots=sorted((s for s in data.get('Time_Slots',[]) if facts.norm(s.get('day'))==day), key=lambda r:facts.int_value(r.get('period')))
    if not slots:return f'No timetable periods are defined for {facts.DAY_NAMES.get(day,day)}.'
    busy={facts.int_value(r.get('period')):r for r in rows if facts.norm(r.get('day'))==day and facts._belongs(r,group,catalog)}
    q=facts.normalized(question)
    if re.search(r'\b(first|earliest|last|latest)\b',q) and re.search(r'\b(class|lecture|session)\b',q):
        sessions=facts._merge_sessions([r for r in rows if facts.norm(r.get('day'))==day and facts._belongs(r,group,catalog)])
        if not sessions:return f'**{group} has no scheduled classes** on {facts.DAY_NAMES.get(day,day)}.'
        first=not bool(re.search(r'\b(last|latest)\b',q))
        session=(min if first else max)(sessions,key=lambda s:facts.int_value(s.get('period')))
        return f'The **{"first" if first else "last"} class** for **{group}** on {facts.DAY_NAMES[day]} is **{facts.norm(session.get("course_name"))}** (Period {session.get("display_period")}), taught by **{facts.norm(session.get("faculty_name"))}**.'
    if ent.get('period') is not None or ent.get('clock') is not None:
        period=ent['period']
        if ent.get('clock') is not None:
            slot=facts._slot_for_clock_time(data,day,ent['clock'])
            if not slot:return f'That time is outside the teaching periods on {facts.DAY_NAMES[day]}.'
            actual=facts.int_value(slot.get('period'))
            if period is not None and actual!=period:
                return f'The requested clock time is Period {actual}, not Period {period}. Which should I check?'
            period=actual
        requested=next((r for r in slots if facts.int_value(r.get('period'))==period),None)
        if requested is None:return f'Period {period} does not exist on {facts.DAY_NAMES[day]}.'
        if period in busy:
            r=busy[period]
            return f'**No — {group} has a scheduled class** on {facts.DAY_NAMES[day]}, Period {period}: {facts.norm(r.get("course_name"))}.'
        return f'**Yes — {group} has no scheduled class** on {facts.DAY_NAMES[day]}, Period {period}. Student-level elective overlaps may still apply.'
    free=[s for s in slots if facts.int_value(s.get('period')) not in busy]
    if not free:return f'**{group} has no unallocated periods** on {facts.DAY_NAMES[day]}.'
    names=', '.join(f'P{facts.int_value(s.get("period"))}' for s in free)
    return f'**{group} has {len(free)} period(s) without a scheduled class on {facts.DAY_NAMES[day]}:** {names}. These are section-level openings; elective-group memberships may need separate checks.'


def _room_utilization(question, ent, rows, data):
    q=facts.normalized(question); day=ent['day']
    catalog={facts.norm(r.get('room_id')):r for r in data.get('Rooms',[])}
    if not catalog:return 'The workbook has no room inventory to analyze.'
    by_room=Counter(facts.norm(r.get('room_id')) for r in rows if facts.norm(r.get('room_id')) and (not day or facts.norm(r.get('day'))==day))
    if re.search(r'\b(unused|never|completely free|entire day|all day)\b',q):
        if not day:return 'Which day should I check for rooms that remain unused?'
        rooms=sorted(set(catalog)-set(by_room))
        return f'**{len(rooms)} room(s) have no scheduled classes on {facts.DAY_NAMES[day]}:** '+(', '.join(rooms) if rooms else 'None')+'.'
    if re.search(r'\b(most|busiest|least|fewest|highest)\b',q):
        mode=min if re.search(r'\b(least|fewest)\b',q) else max
        loads={k:by_room[k] for k in catalog}
        value=mode(loads.values())
        winners=sorted(k for k,v in loads.items() if v==value)
        return f'**{", ".join(winners[:8])}** {"is" if len(winners)==1 else "are"} {"least" if mode==min else "most"} used'+(f' on {facts.DAY_NAMES[day]}' if day else ' during the week')+f': {value} occupied period(s).' + (f' ({len(winners)-8} additional tied rooms.)' if len(winners)>8 else '')
    room_match=facts.match_value(question,catalog)
    if room_match:
        count=by_room[room_match]
        return f'**{room_match} is occupied for {count} scheduled period(s)**'+(f' on {facts.DAY_NAMES[day]}' if day else ' this week')+'.'
    return 'Ask which room is most used, which rooms are unused on a day, or how often a particular room is scheduled.'


def _teacher_comparison(question, rows, data):
    people=facts._named_teachers_in_question(question,router._teachers(data,rows))
    if len(people)!=2:return 'Please name the two teachers you want to compare.'
    lines=[]
    for name in people:
        selected=[r for r in rows if facts.norm(r.get('faculty_name'))==name]
        lines.append(f'- **{name}:** {len(selected)} teaching periods, {len(facts._merge_sessions(selected))} sessions')
    return '**Teaching workload comparison:**\n\n'+'\n'.join(lines)+'\n\nThis compares scheduled periods, not teaching quality or contracted workload.'



def _course_teacher_confirmation(question, ent, rows, data):
    teacher,course,group=(ent[k] for k in ('teacher','course','group'))
    if not teacher or not course:return None
    code_groups={facts.norm(g.get('group_id')):g for g in data.get('Student_Groups',[])}
    hits=[r for r in rows if facts.norm(r.get('faculty_name'))==teacher
          and facts.norm(r.get('course_id'))==course
          and (not group or facts._belongs(r,group,code_groups))]
    if not hits:
        return f'**No.** {teacher} is not assigned to **{course}**'+(f' for **{group}**' if group else '')+' in the selected timetable.'
    names=sorted({facts.norm(r.get('group_id')).split('__')[0] for r in hits})
    return f'**Yes.** {teacher} teaches **{course}**'+(f' for **{group}**' if group else '')+'. Sections: '+', '.join(names)+'.'


def _room_occupancy(question, ent, rows, data):
    rooms={facts.norm(r.get('room_id')):r for r in data.get('Rooms',[])}
    room=facts.match_value(question,rooms)
    if not room:
        cleaned=re.sub(r'[^a-z0-9]','',question.lower())
        matches=[name for name in rooms if re.sub(r'[^a-z0-9]','',name.lower()) in cleaned]
        room=matches[0] if len(matches)==1 else None
    if not room:return None
    day,period,clock=ent['day'],ent['period'],ent['clock']
    if not day:return f'Which day should I check for **{room}**?'
    if clock is not None:
        sl=facts._slot_for_clock_time(data,day,clock)
        if sl is None:return f'That time is outside the teaching periods on {facts.DAY_NAMES[day]}.'
        resolved=facts.int_value(sl.get('period'))
        if period is not None and period!=resolved:
            return f'The clock time means Period {resolved}, not Period {period}. Which should I check?'
        period=resolved
    matching=[r for r in rows if facts.norm(r.get('room_id'))==room and facts.norm(r.get('day'))==day]
    if period is not None:
        hits=[r for r in matching if facts.int_value(r.get('period'))==period]
        if not hits:return f'**Yes — {room} is unallocated** on {facts.DAY_NAMES[day]}, Period {period}. Room-only availability does not guarantee a feasible class move.'
        item=hits[0]
        return (f'**No — {room} is occupied** on {facts.DAY_NAMES[day]}, Period {period} '
                f'by **{facts.norm(item.get("course_id"))}** for **{facts.norm(item.get("group_id"))}**.')
    periods=sorted({facts.int_value(r.get('period')) for r in matching})
    slots=sorted({facts.int_value(sl.get('period')) for sl in data.get('Time_Slots',[])
                  if facts.norm(sl.get('day'))==day})
    free=[p for p in slots if p not in periods]
    return f'**{room}** on {facts.DAY_NAMES[day]} — occupied: '+(', '.join('P'+str(p) for p in periods) or 'none')+'; unallocated: '+(', '.join('P'+str(p) for p in free) or 'none')+'.'

def _other_teachers(question, ent, rows, data, conversation):
    course=ent.get('course')
    if not course:return 'Which course do you mean by “other teachers”?'
    matches=sorted({facts.norm(r.get('faculty_name')) for r in rows if facts.norm(r.get('course_id'))==course and facts.norm(r.get('faculty_name'))})
    if not matches:return f'No scheduled teachers found for **{course}** in the selected timetable.'
    # An explicit name can be excluded, otherwise recover the previously
    # mentioned teacher only when they were uniquely named in the last answer.
    named=facts.match_teacher(question,matches)[0]
    if not named:
        last=ent['memory']['last_assistant']
        referenced=[t for t in matches if facts.match_value(last,(t,))]
        if len(referenced)==1: named=referenced[0]
    listed=[n for n in matches if facts.match_value(ent['memory']['last_assistant'],(n,))]
    others=[n for n in matches if n!=named] if named else [n for n in matches if n not in listed]
    if len(matches)==1:
        return f'**No.** {matches[0]} is the only scheduled teacher for **{course}** in the selected timetable.'
    if named:
        return f'**Yes.** Apart from {named}, {len(others)} other scheduled teacher(s) teach **{course}**: '+', '.join(others)+'.'
    if listed and not others:
        return f'**No additional teachers.** The selected timetable lists only the {len(matches)} already mentioned for **{course}**.'
    if listed and others:
        return f'**Yes.** {len(others)} additional teacher(s) teach **{course}**: '+', '.join(others)+'.'
    return f'**{course} has {len(matches)} scheduled teachers:** '+', '.join(matches)+'.'



def _validated_alternatives(session, rows, data):
    """Ask the existing independent validator for genuine one-session options.

    Read-only. This deliberately does not call SQLite, submit a request or
    approve anything; it does not claim student-ID-level conflict verification.
    """
    from timetable_rescheduling import propose_moves
    sid=facts.norm(session.get('session_id'))
    if not sid:return 'This session does not have a source session ID, so I cannot validate alternative placements.'
    real_rooms={facts.norm(x.get('room_id')) for x in data.get('Rooms',[])}
    extras={facts.norm(r.get('room_id')) for r in rows if facts.norm(r.get('room_id')).startswith('SYNTHETIC_LAB_')
            and facts.norm(r.get('room_id')) not in real_rooms}
    extra_labs=max((int(m.group(1)) for r in extras if (m:=re.fullmatch(r'SYNTHETIC_LAB_(\d+)',r))),default=0)
    include_mentoring=any(facts.norm(r.get('course_id'))=='MME001' or 'MENTOR' in facts.norm(r.get('kind')).upper() for r in rows)
    try:
        result=propose_moves(data,rows,sid,include_mentoring=include_mentoring,
                             extra_simulated_labs=extra_labs,maximum_options=5)
    except (ValueError,KeyError,TypeError,IndexError) as exc:
        return ('I could not independently validate alternative moves for this selected '
                'timetable. The current schedule is unchanged. Open **Coordinator workflows** '
                'to review the source data and constraint report.')
    if result.get('status')!='OPTIONS_FOUND':
        why=result.get('reasons') or [str(result.get('status') or 'No verified options')]
        return ('**No independently verified move suggestions are available.** '
                +'; '.join(str(x) for x in why[:3])
                +' The selected timetable has not been changed.')
    options=result['options'][:5]
    lines=[]
    for o in options:
        lines.append(f'- **{o.get("slot",o.get("slot_id"))}**, room **{o.get("room_id")}**'
                     + (f' (quality score change {o["score_change"]:+d})' if isinstance(o.get('score_change'),int) else ''))
    return ('**Independently validated alternative slots for '+facts.norm(session.get('course_id'))+' / '
            +facts.norm(session.get('group_id'))+'** (one-session move):\n\n'
            +'\n'.join(lines)
            +'\n\nThese are proposals, **not approved timetable changes**. '
            'Submit the exact move in **Coordinator workflows** for approval. '
            'Student memberships missing from the workbook cannot be verified.')

def _pending_move(question, ent, rows, data, conversation):
    """Complete an incomplete multi-turn move request; no implicit edits."""
    history=ent['memory']['pending_move']
    basis=(history+' '+question).strip() if history else question
    all_session_ids={sid for r in rows if (sid:=facts.norm(r.get('session_id'))) and len(sid)>=4 and re.search(r'\d',sid)}
    requested_id=facts.match_value(question,all_session_ids)
    exact_record=next((r for r in rows if facts.norm(r.get('session_id'))==requested_id),None) if requested_id else None
    catalog=router._codes(data,rows)
    course,amb=facts.match_course_reference(basis,catalog,rows)
    if amb:return 'Which course do you mean: '+', '.join(amb)+'?'
    if not course and exact_record: course=facts.norm(exact_record.get('course_id'))
    if not course:return 'Which course should be moved? Please give the course code or name.'
    group=facts._group_match(question, {g:{} for g in router._groups(data,rows)},rows) or facts._group_match(history, {g:{} for g in router._groups(data,rows)},rows)
    practical=bool(re.search(r'\b(practical|laborator(?:y|ies)|lab|batch)\b',basis,re.I))
    if exact_record and not group:group=facts.norm(exact_record.get('group_id'))
    selected=facts._merge_sessions([r for r in rows if facts.norm(r.get('course_id'))==course
                                    and (not practical or facts.norm(r.get('kind')).upper()=='PRACTICAL')
                                    and (not group or facts.norm(r.get('group_id'))==group)
                                    and (not requested_id or facts.norm(r.get('session_id'))==requested_id)])
    # 'BCL203 ... for Monday' names the CURRENT class in an ongoing move
    # conversation. Filter the three weekly course sessions before asking
    # which session to move; a destination must explicitly say 'to/into'.
    destination_phrase = bool(re.search(r'\b(?:to|into)\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|MON|TUE|WED|THU|FRI)\b',question,re.I))
    if not destination_phrase:
        origin_day = facts.parse_day(question)
        if origin_day:
            selected = [r for r in selected if facts.norm(r.get('day')).upper() == origin_day]
        origin_period = facts.parse_period(question) or facts._ordinal_period(question)
        if origin_period is not None:
            selected = [r for r in selected if facts.int_value(r.get('period')) == origin_period]
    if not selected:return f'No matching session for **{course}**'+(f' / {group}' if group else '')+' exists in the selected timetable.'
    if not group and len({facts.norm(r.get('group_id')) for r in selected})>1:
        groups=sorted({facts.norm(r.get('group_id')) for r in selected})
        return f'**{course} has {len(groups)} possible teaching groups.** Which one do you mean? '+', '.join(groups[:7])+(f', and {len(groups)-7} more.' if len(groups)>7 else '.')
    if len(selected)>1:
        return f'**{course} / {group or "selected group"}** has {len(selected)} sessions. Which current day/session should I use?'
    session=selected[0]
    if re.search(r'\b(alternatives?|another slot|other times?|better times?|suggest|recommend|options?)\b',question,re.I):
        return _validated_alternatives(session,rows,data)
    day=facts.parse_day(question) or facts.parse_day(history)
    period=facts.parse_period(question) or facts.parse_period(history)
    clock=facts.parse_clock_minutes(question)
    if clock is None:clock=facts.parse_clock_minutes(history)
    if day and clock is not None and period is None:
        slot=facts._slot_for_clock_time(data,day,clock)
        if slot:period=facts.int_value(slot.get('period'))
    if not day or not period:
        return f'**{course} / {facts.norm(session.get("group_id"))}** identified. Which new day and starting period should I check?'
    duration=len([r for r in rows if facts.norm(r.get('session_id'))==facts.norm(session.get('session_id'))])
    duration=max(1,duration)
    if period+duration-1 > max((facts.int_value(s.get('period')) for s in data.get('Time_Slots',[]) if facts.norm(s.get('day'))==day),default=0):
        return f'**No —** this session needs {duration} consecutive periods, and the requested start does not leave enough time on {facts.DAY_NAMES[day]}.'
    # Fast, grounded preflight; the full validator remains the final authority.
    import timetable_policies
    from timetable_policies import effective_group_semester
    policies=timetable_policies.load_policies(data)
    groups={facts.norm(g.get('group_id')):g for g in data.get('Student_Groups',[])}
    semester=effective_group_semester(facts.norm(session.get('group_id')),groups)
    slots=[s for s in data.get('Time_Slots',[]) if facts.norm(s.get('day'))==day and
           period <= facts.int_value(s.get('period')) < period+duration]
    if len(slots)!=duration:return f'**No —** the requested {duration}-period block is not defined on {facts.DAY_NAMES[day]}.'
    blockers=[]
    for slot in slots:
        sid=facts.norm(slot.get('slot_id'))
        if sid in policies.hard_closed_slot_ids:
            blockers.append('institutional hard-closed period')
        if policies.reserved(slot, semester,course in policies.window_allowed_course_codes):
            blockers.append('reserved open-elective window')
        if facts.norm(slot.get('academic_classes_allowed')).lower() not in ('yes','true','y','1'):
            blockers.append('academic classes are disallowed in the requested period')
    target=facts.norm(session.get('session_id'))
    others=[r for r in rows if facts.norm(r.get('session_id'))!=target and facts.norm(r.get('day'))==day
            and period<=facts.int_value(r.get('period'))<period+duration]
    if any(facts.norm(r.get('faculty_name'))==facts.norm(session.get('faculty_name')) for r in others):
        blockers.append('the teacher has another class')
    # One overlapping group is enough to reject; this is not a full validator.
    if any(facts.norm(r.get('group_id'))==facts.norm(session.get('group_id')) for r in others):
        blockers.append('the same group has another class')
    where=f'**{course} — {facts.norm(session.get("group_id"))}**, {facts.DAY_NAMES.get(day,day)} Periods {period}'
    if duration>1:where+=f'–{period+duration-1}'
    if blockers:
        return f'**Not suitable for this class move.** {where} conflicts with '+', '.join(dict.fromkeys(blockers))+'. No change has been made. The complete duration and all constraints require independent validation. You can ask about another time or submit a different slot for coordinator review.'
    return f'**Initial checks found no immediate teacher/group/policy conflict** for {where}. **This is not yet a validated move**: the laboratory/classroom, capacity, other group overlaps and all hard constraints must be checked in **Coordinator workflows** before approval. No change has been made.'


def _constraint_explanation(question):
    q=facts.normalized(question)
    if 'room' in q or 'classroom' in q or 'lab' in q:
        return 'A room cannot hold two overlapping classes. A practical also needs a suitable laboratory with enough seats and consecutive periods. A free room alone does not prove a class move is valid.'
    if 'teacher' in q or 'faculty' in q:
        return 'One teacher cannot be assigned to two overlapping sessions. The system also checks the teacher’s availability, student-group overlap and applicable institutional rules.'
    return 'A proposed class must satisfy teacher, room, student-group, duration and hard institutional constraints. Soft preferences influence optimization but do not automatically forbid a class.'



def _dataset_query(question, ent, rows, data, llm=None):
    """A reusable safe query over timetable rows: filter, group, count or list.

    The LLM may pick the operation but cannot invent filters. Teacher, course,
    room, section, day and period must resolve against real dataset values.
    Result values are always calculated in Python from the selected CSV.
    """
    llm=llm if isinstance(llm,dict) else {}
    q=facts.normalized(question)
    teacher,course,group,day=(ent[k] for k in ('teacher','course','group','day'))
    group_catalog={facts.norm(x.get('group_id')):x for x in data.get('Student_Groups',[])}
    room_catalog={facts.norm(x.get('room_id')) for x in data.get('Rooms',[])}
    room=facts.match_value(question,room_catalog)
    raw_ids=re.findall(r'\b(?:RN|LAB|SYNTHETIC_LAB)[\s_-]*[A-Z0-9_-]+\b',question,re.I)
    unknown_ids=[candidate for candidate in raw_ids if not any(
        re.sub(r'[^a-z0-9]', '', candidate.lower())==re.sub(r'[^a-z0-9]', '', identifier.lower())
        for identifier in room_catalog)]
    if unknown_ids and not room:
        return f'I cannot find **{unknown_ids[0]}** in the selected university workbook. Please check the room ID.'
    # Accept an LLM room only when it is a real inventory identifier present in
    # the user's text. A confident model cannot pick a different room.
    if not room and str(llm.get('room') or '') in room_catalog:
        proposal=str(llm['room'])
        if facts.match_value(question,(proposal,))==proposal: room=proposal
    period=ent.get('period')
    if ent.get('clock') is not None and day:
        slot=facts._slot_for_clock_time(data,day,ent['clock'])
        if slot is None:return f'That clock time is outside the defined periods on {facts.DAY_NAMES[day]}.'
        actual=facts.int_value(slot.get('period'))
        if period is not None and actual!=period:
            return f'The clock time refers to Period {actual}, not Period {period}. Which should I check?'
        period=actual
    kind=None
    if re.search(r'\b(practical|laboratory classes|lab sessions)\b',q):kind='PRACTICAL'
    elif re.search(r'\b(theory|lectures)\b',q):kind='THEORY'
    elif str(llm.get('kind') or '').upper() in ('PRACTICAL','THEORY','MENTOR') and re.search(r'\b(kind|type|session)\b',q):
        kind=str(llm['kind']).upper()
    filtered=[r for r in rows
        if (not teacher or facts.norm(r.get('faculty_name'))==teacher)
        and (not course or facts.norm(r.get('course_id'))==course)
        and (not group or facts._belongs(r,group,group_catalog))
        and (not day or facts.norm(r.get('day'))==day)
        and (not room or facts.norm(r.get('room_id'))==room)
        and (not kind or facts.norm(r.get('kind')).upper()==kind)
        and (period is None or facts.int_value(r.get('period'))==period)]
    if period is not None and filtered:
        # A two-period practical remains one whole session.
        full_ids={facts.norm(r.get('session_id')) for r in filtered}
        filtered=[r for r in rows if facts.norm(r.get('session_id')) in full_ids]
    sessions=facts._merge_sessions(filtered)
    aggregation=str(llm.get('aggregate') or '').lower()
    if aggregation not in ('count','list','first','last','breakdown','top','bottom'):
        aggregation='top' if re.search(r'\b(most|busiest|highest|greatest)\b',q) else ('bottom' if re.search(r'\b(least|fewest)\b',q) else ('count' if re.search(r'\b(how many|count|number|total)\b',q) else 'list'))
    dimensions={'teacher':'faculty_name','faculty':'faculty_name',
                'course':'course_id','subject':'course_id',
                'section':'group_id','group':'group_id',
                'room':'room_id','day':'day','kind':'kind'}
    dimension=str(llm.get('group_by') or '').lower()
    if dimension not in dimensions:
        if re.search(r'\b(which|who|list|most|least)\b',q) and re.search(r'\b(teachers?|faculty|instructors?)\b',q):dimension='teacher'
        elif re.search(r'\b(which|list|most|least)\b',q) and re.search(r'\b(sections?|groups?|batches?)\b',q):dimension='section'
        elif re.search(r'\b(which|list|most|least)\b',q) and re.search(r'\b(rooms?|labs?)\b',q):dimension='room'
        elif re.search(r'\b(which|list|most|least)\b',q) and re.search(r'\b(courses?|subjects?)\b',q):dimension='course'
    filters=[x for x in (teacher,course,group,day,room,kind) if x]
    descriptor=', '.join(filters) if filters else 'the selected timetable'
    if dimension in dimensions and aggregation in ('list','breakdown','count','top','bottom'):
        by=defaultdict(list)
        for s in sessions:
            key=facts.norm(s.get(dimensions[dimension]))
            if dimension=='section' and '__' in key:key=key.split('__')[0]
            if key:by[key].append(s)
        if not by:return f'No matching {dimension} assignments were found for **{descriptor}**.'
        details=sorted(by.items(), key=lambda x:(-len(x[1]),x[0]))
        maxrows=15
        if aggregation=='count':return f'**{len(details)} distinct {dimension} values** match {descriptor}.'
        if aggregation in ('top','bottom'):
            goal=(max if aggregation=='top' else min)(len(value) for value in by.values())
            winners=sorted(key for key,value in by.items() if len(value)==goal)
            return f'**{", ".join(winners[:12])}** {"has" if len(winners)==1 else "have"} the {"most" if aggregation=="top" else "fewest"} matching sessions for {descriptor}: **{goal}**.'
        # 'List teachers for section X' asks for people, not the number of
        # timetable session rows per person. Keep session counts available
        # when the user explicitly asks about teaching load or counts.
        if (dimension=='teacher' and group and aggregation=='list'
                and re.search(r'\b(list|which|who|show)\b',q)
                and re.search(r'\b(teachers?|faculty|instructors?)\b',q)
                and not re.search(r'\b(count|sessions?|how many|workload)\b',q)):
            return (f'**{len(details)} teachers teach {group}:**\n\n'
                    + '\n'.join('- '+facts._faculty_display_name(n) for n,_ in sorted(details)))
        listing='\n'.join(f'- **{facts._faculty_display_name(name) if dimension=="teacher" else name}** — {len(ss)} session(s)' for name,ss in details[:maxrows])
        remain=(f'\n\n{len(details)-maxrows} more values. Narrow the question by day or section.' if len(details)>maxrows else '')
        return f'**{len(details)} {dimension} values for {descriptor}:**\n\n'+listing+remain
    if aggregation=='count':
        if re.search(r'\b(periods?|teaching hours?)\b',q):
            return f'**{len(filtered)} occupied teaching period(s)** match {descriptor}. ({len(sessions)} distinct sessions.)'
        return f'**{len(sessions)} distinct teaching session(s)** match {descriptor}. ({len(filtered)} occupied period rows.)'
    if not sessions:return f'No scheduled classes match **{descriptor}**.'
    if aggregation in ('first','last'):
        chosen=sessions[0] if aggregation=='first' else sessions[-1]
        return ('**'+('First' if aggregation=='first' else 'Last')+' matching class:**\n\n'
                +facts._format_session(chosen))
    # Bound output size to avoid a 600-session dump, even if the LLM requests
    # an extreme length. Users can narrow their question interactively.
    limit=15 if re.search(r'\b(all|every|complete|full list)\b',q) else 8
    try:limit=min(limit,max(1,int(llm.get('limit',limit))))
    except (TypeError,ValueError):pass
    listing='\n'.join('- '+facts._format_session(s) for s in sessions[:limit])
    suffix=f'\n\nShowing {limit} of {len(sessions)}. Ask for a specific day, teacher, course or section to narrow it down.' if len(sessions)>limit else ''
    return f'**{len(sessions)} matching teaching session(s) for {descriptor}:**\n\n'+listing+suffix

def respond(question, rows, data, report=None, *, conversation=(), llm_enabled=False,
            model='openai/gpt-oss-120b'):
    """Interpret -> ground slots -> call verified tool -> return short answer.

    Returns None only when legacy specialist queries may be better equipped.
    This function itself never mutates `rows` or `data`.
    """
    if not (question or '').strip():return None
    from timetable_spelling import normalize_question
    spelling=normalize_question(question,data,rows)
    if spelling.clarification:return spelling.clarification
    question=spelling.question
    ent=_entities(question,rows,data,conversation)
    memory=ent['memory']
    offline=_question_intent(question)
    original=router._local_intent(question)
    lower=facts.normalized(question)
    # V18.5: compose verified multi-part answers before the single-intent
    # handlers. This is necessary even when Groq is switched on: a single
    # intent label cannot represent 'all sections AND Wednesday sections'.
    from timetable_chat_queries import answer_multi_question
    composed = answer_multi_question(question, rows, data, ent)
    if composed is not None:
        return composed
    # Keep the long-established specialist course+teacher+section report and
    # the optional V12 classifier intact where they already work well.
    if ent['course'] and not ent['teacher'] and re.search(r'\b(who|teachers?|faculty)\b',lower) and re.search(r'\b(sections?|batches|groups)\b',lower):
        return None
    if llm_enabled and original == 'course_teachers' and (
        (ent['teacher'] and not ent['course']) or
        ('responsible' in lower and 'module' in lower)):
        return None
    # Short replies to a pending class-move discussion preserve ALL essential
    # slots, not only the last displayed subject and person.
    pending_followup=bool(memory.get('pending_move') and ent.get('follow')
        and not re.search(r'\b(who|which|what|where|when|find|show|list|can|does|how|free|available)\b',lower))
    if re.search(r'\b(what can|what all|can you help|what questions|how can you|what are your capabilities)\b',lower) and re.search(r'\b(ask|do|help|answer|capabilities|questions)\b',lower):
        return router._read_only_dispatch('help',ent,question,rows,data,report,None)
    if ent.get('group') and not ent.get('teacher') and re.search(r'\b(free|available|vacant|unallocated|spare)\b',lower) and not re.search(r'\b(rooms?|labs?|teachers?|faculty)\b',lower):
        return _section_free(question,ent,rows,data)
    if re.search(r'\b(labs?|laborator(?:y|ies)|classrooms?|rooms?)\b',lower) and re.search(r'\b(can i use|can we use|can i book|could we use)\b',lower) and ent.get('day'):
        return router._read_only_dispatch('room_availability',ent,question,rows,data,report,None)
    if ent.get('teacher') and re.search(r'\b(what else|anything else|other subjects|other courses)\b',lower) and not re.search(r'\b(teachers?|faculty|staff)\b',lower):
        return router._read_only_dispatch('teacher_courses',ent,question,rows,data,report,None)
    if (ent.get('teacher') and re.search(r'\b(subjects?|courses?|modules?)\b',lower)
            and re.search(r'\b(taught|teach(?:es|ing)?|handled|handle(?:s)?|instruct(?:s|ed|ing)?|assigned)\b',lower)
            and not re.search(r'\b(sections?|batches?|groups?)\b',lower)
            and not re.search(r'\b(how many|number of|count|total)\b',lower)):
        return router._read_only_dispatch('teacher_courses',ent,question,rows,data,report,None)
    if offline=='move_guidance' or (original=='move_guidance') or pending_followup:
        return _pending_move(question,ent,rows,data,conversation)
    if offline=='course_teachers' and re.search(r'\b(another|other|else|besides|apart from|only one)\b', lower):
        return _other_teachers(question,ent,rows,data,conversation)
    if re.search(r'\b(what else|any more|anyone else)\b',lower) and memory.get('course'):
        return _other_teachers(question,ent,rows,data,conversation)
    special=offline if offline and offline!='move_guidance' else None
    # When a user is *supplying a value* in response to a clarification,
    # the active task is more authoritative than a fresh LLM guess. Otherwise
    # 'for period 2' can be mistranslated as a generic schedule request.
    if (original == 'unknown' and ent.get('follow') and
            memory.get('intent') in router.INTENTS and
            memory['intent'] != 'unknown'):
        special = memory['intent']
    # Make Ollama a real intent planner: when Python lacks a clear operation,
    # it is consulted, but it cannot forge names or scheduling facts.
    llm=None
    model_needed=(original=='unknown' or (original=='course_teachers' and
                 not ent.get('course') and not ent.get('teacher')))
    if llm_enabled and model_needed and not special:
        llm=router._call_qwen_intent(question,teacher_names=router._teachers(data,rows),
                                    course_codes=router._codes(data,rows),
                                    recent_question=memory.get('last_user',''),model=model)
        if llm and llm.get('intent') in TOOL_NAMES and model_needed:
            special=llm['intent']
    if ent.get('day') and re.search(r'\b(rooms?|labs?|laborator(?:y|ies)|classrooms?|rn)\b',lower) and re.search(r'\b(occupied|busy|booked)\b',lower):
        single=_room_occupancy(question,ent,rows,data)
        if single:return single
    if ent.get('teacher') and re.search(r'\b(how many|number of|count|total)\b',lower) and re.search(r'\b(courses?|subjects?|modules?)\b',lower):
        unique={facts.norm(r.get('course_id')) for r in rows if facts.norm(r.get('faculty_name'))==ent['teacher']}
        return f'**{ent["teacher"]} teaches {len(unique)} distinct course(s)** in the selected timetable: '+', '.join(sorted(unique))+'.'
    if ent.get('teacher') and re.search(r'\b(how many|number of|count|total)\b',lower) and re.search(r'\b(sections?|cohorts?)\b',lower):
        unique={facts.norm(r.get('group_id')).split('__')[0] for r in rows if facts.norm(r.get('faculty_name'))==ent['teacher']}
        return f'**{ent["teacher"]} teaches {len(unique)} distinct section(s)** in the selected timetable.'
    if special=='teacher_workload':return _teacher_load(question,ent,rows,data)
    if special=='section_availability':return _section_free(question,ent,rows,data)
    if special=='room_utilization':return _room_utilization(question,ent,rows,data)
    if special=='teaching_comparison':return _teacher_comparison(question,rows,data)
    if special=='explain_constraints':return _constraint_explanation(question)
    if special=='dataset_query' and (model_needed or re.search(r'\b(most|least|how many|count|number of|occupied|booked)\b',lower)):
        return _dataset_query(question,ent,rows,data,llm)
    if ent.get('teacher') and ent.get('course') and re.search(r'\b(does|is|has|assigned to|responsible for)\b',lower) and not re.search(r'\b(when|what time|which periods?)\b',lower):
        return _course_teacher_confirmation(question,ent,rows,data)
    if re.search(r'\b(rooms?|labs?|laborator(?:y|ies)|classrooms?|rn)\b',lower) and re.search(r'\b(free|vacant|available|occupied|busy|used|allocated)\b',lower) and (ent.get('day') or re.search(r'\b(which day|what day)\b',lower)):
        single=_room_occupancy(question,ent,rows,data)
        if single:return single
    # Generic filter/aggregation tool for flexible questions such as 'How many
    # practical sessions does section X have?' and 'Which teachers use room Y?'.
    if re.search(r'\b(how many|count|number of|which|who|list|show)\b',lower) and (
        (ent.get('group') and re.search(r'\b(practicals?|lab sessions?|theory sessions?)\b',lower)) or
        (re.search(r'\b(room|lab)\b',lower) and re.search(r'\b(teach|use|scheduled|classes)\b',lower) and not re.search(r'\b(free|available)\b',lower))):
        return _dataset_query(question,ent,rows,data,llm)
    # Multi-part questions use two tools, rather than answering only one half.
    if ent.get('teacher') and re.search(r'\b(courses?|subjects?)\b',lower) and re.search(r'\b(sections?|groups?|batches?)\b',lower) and re.search(r'\b(teach|handle|assigned|which|what)\b',lower):
        a=router._read_only_dispatch('teacher_courses',ent,question,rows,data,report,llm)
        b=router._read_only_dispatch('teacher_sections',ent,question,rows,data,report,llm)
        return (a+'\n\n'+b) if a and b else a or b
    intent = special if special in router.INTENTS else (original if original in router.INTENTS else 'unknown')
    # Clarification replies contain *values*, not a full sentence. Continue
    # the pending operation with the newly supplied teacher/day/period. This
    # handles "free rooms Monday" -> "for period 2", and "free Friday" ->
    # "Ashima Rani" without reverting to a generic timetable dump.
    if (intent == 'unknown' and ent.get('follow') and
            memory.get('intent') in router.INTENTS and memory['intent'] != 'unknown'):
        intent = memory['intent']
    # These specialist legacy paths already preserve important distinctions:
    # a full teacher schedule with no specified day, and explicit impossible
    # clock times. Avoid losing that detail to generic clarifications.
    if intent == 'schedule' and not ent['day'] and re.search(r'\bwhen\s+(?:does|will)\b',question,re.I):
        return None
    if intent == 'teacher_availability' and ent['clock'] is not None and ent['clock'] < 8*60:
        return None
    # Teacher+sections means the teacher directory; course+sections means the
    # course directory including section allocations.
    if ent['course'] and not ent['teacher'] and intent == 'teacher_sections':
        intent = 'course_teachers'
    if ent['course'] and not ent['teacher'] and intent == 'teacher_courses':
        intent = 'course_teachers'
    if intent!='unknown':
        response=router._read_only_dispatch(intent,ent,question,rows,data,report,llm)
        if response is not None:
            if intent == 'teacher_availability' and re.search(r'\b(maam|madam|meet)\b',lower) and ent['day'] and ent['period'] is None and ent['clock'] is None:
                response=response.replace(' is available on ', ' is available to meet on ', 1)
            return response
    # Allow existing specialised tools (multi-day availability, specific named
    # validation diagnostics) to answer; unknown questions get a useful
    # clarification rather than a giant session listing.
    return None
