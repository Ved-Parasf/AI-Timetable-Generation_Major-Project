"""Opt-in AI task planning and verified timetable analytics for Timora.

The cloud model can select an operation or a *preference* only. Every teacher,
room, time, option, conflict and published CSV originates in the currently
selected workbook/timetable and independently validated rescheduler.
"""
from __future__ import annotations

import re

# Conservative corrections for intent verbs. Do not fuzzy-correct names, IDs,
# periods, or institutional policy terms here.
_ACTION_TYPOS = {
    'reschedu': 'reschedule', 'reschedue': 'reschedule',
    'reschedle': 'reschedule', 'reshedule': 'reschedule',
    'reshedu': 'reschedule', 'reshedulee': 'reschedule',
    'shft': 'shift', 'shif': 'shift', 'shifft': 'shift',
    'mov': 'move', 'moove': 'move', 'relocte': 'relocate',
}


def normalize_task_text(text):
    """Normalize action vocabulary without changing faculty/course identifiers."""
    return re.sub(r'\b(?:' + '|'.join(map(re.escape, _ACTION_TYPOS)) + r')\b',
                  lambda m: _ACTION_TYPOS[m.group(0).lower()], str(text or ''),
                  flags=re.IGNORECASE)


def all_available_faculty_answer(question, rows, data):
    """Answer a *plural* faculty availability lookup using workbook restrictions.

    An unscheduled teacher is NOT necessarily free: explicit faculty
    availability records must affirm the requested slot. Unknowns are excluded.
    Returns None for other queries so existing routing remains undisturbed.
    """
    import timetable_assistant as facts
    q = question.lower()
    if not (re.search(r'\b(?:teachers?|faculty|professors?|lecturers?|instructors?|staff)\b', q)
            and re.search(r'\b(?:free|available|vacant|unoccupied)\b', q)
            and re.search(r'\b(?:which|who|list|show|all|any|find|give|how many)\b', q)):
        return None
    # A named-person question is not a request for an all-faculty report.
    faculty_by_name = {facts.norm(f.get('faculty_name')): f for f in data.get('Faculty', [])
                       if facts.norm(f.get('faculty_name'))}
    roster = sorted(set(faculty_by_name) | {facts.norm(r.get('faculty_name')) for r in rows
                                               if facts.norm(r.get('faculty_name'))})
    if facts._named_teachers_in_question(question, roster):
        return None
    day = facts.parse_day(question)
    if not day:
        return 'Which weekday should I check for available faculty?'
    period = facts.parse_period(question)
    clock = facts.parse_clock_minutes(question)
    if clock is not None:
        slot = facts._slot_for_clock_time(data, day, clock)
        if slot is None:
            return 'That time is outside the defined teaching periods for the selected university.'
        resolved = facts.int_value(slot.get('period'))
        if period is not None and period != resolved:
            return f'The requested time belongs to Period {resolved}, not Period {period}. Which one should I use?'
        period = resolved
    if period is None:
        return 'Which period should I check for available faculty on ' + facts.DAY_NAMES.get(day, day) + '?'
    allowed = {facts.int_value(s.get('period')) for s in data.get('Time_Slots', [])
               if facts.norm(s.get('day')).upper() == day}
    if period not in allowed:
        return f'Period {period} is not defined on {facts.DAY_NAMES.get(day, day)} in this workbook.'
    free = []
    busy = blocked = unknown = 0
    faculty_index = {facts.norm(f.get('faculty_id')): f for f in data.get('Faculty', [])}
    for teacher in roster:
        _, status = facts._teacher_slot_status(teacher, day, period, rows, data,
                                               faculty_index, clock_minutes=clock)
        if status == 'AVAILABLE':
            free.append(teacher)
        elif status == 'BUSY':
            busy += 1
        elif status == 'BLOCKED':
            blocked += 1
        else:
            unknown += 1
    label = facts.DAY_NAMES.get(day, day)
    if free:
        answer = f'**{len(free)} teachers are confirmed available on {label}, Period {period}:**\n\n'
        answer += ', '.join(sorted(free)) + '.'
    else:
        answer = f'No teachers have *confirmed availability* on {label}, Period {period}.'
    if unknown:
        answer += f'\n\nAvailability was not confirmed for {unknown} other teacher(s); they are not listed as free.'
    answer += '\n\n*Based on the selected timetable and workbook faculty-availability records.*'
    return answer


def retry_incorrect_clarification(question, reply, rows, data, report=None,
                                  *, conversation=(), llm_enabled=False,
                                  model='openai/gpt-oss-120b'):
    """Second chance for a *wrong* clarification, not a second free-text answer.

    The model chooses only an existing read-only operation. Actual entities and
    results are re-resolved by local Python. Even if Groq is unavailable, a
    correct plural availability request has a deterministic implementation.
    """
    if not reply or not isinstance(reply, str):
        return None, False
    lower = reply.lower()
    if not any(x in lower for x in (
        'which teacher should i check', 'which teacher’s', "which teacher's",
        'which course should i check', 'which section should i check',
        'could not identify a supported timetable query',
    )):
        return None, False
    direct = all_available_faculty_answer(question, rows, data)
    if direct is not None:
        return direct, False
    if not llm_enabled:
        return None, False
    try:
        from timetable_cloud_ai import classify_question
        from timetable_nl_router import _selected_entities, _read_only_dispatch
        plan = classify_question(question, model=model)
        if not plan or plan.get('intent') not in {
                'teacher_sections', 'teacher_courses', 'course_teachers',
                'section_courses', 'course_sections', 'teacher_availability',
                'room_availability', 'common_availability', 'schedule',
        }:
            return None, False
        ent = _selected_entities(question, rows, data, conversation=conversation)
        improved = _read_only_dispatch(plan['intent'], ent, question, rows, data, report, plan)
        if improved and improved.strip() != reply.strip() and not any(
                fragment in improved.lower() for fragment in
                ('which teacher should', 'which teacher’s', 'which course should',
                 'which section should')):
            return improved, True
    except (ImportError, AttributeError, TypeError, ValueError, KeyError, OSError):
        pass
    return None, False


def _score_option(option, criterion):
    delta = option.get('delta') or {}
    if criterion == 'total_quality':
        return (float(option.get('rank_cost', 0)), str(option.get('slot_id')))
    return (float(delta.get(criterion, 0)), float(option.get('rank_cost', 0)),
            str(option.get('slot_id')))


def verified_slot_advice(proposal, request='', *, llm_enabled=False,
                         model='openai/gpt-oss-120b', planner=None):
    """Offer only *validated* options; Groq may choose the user's soft priority.

    The model never chooses a new room, slot or unvalidated class move.
    The public answer contains only metrics supplied by the independent
    rescheduler. Returns (message, recommended_slot_id, used_groq).
    """
    options = proposal.get('options') or []
    if proposal.get('status') != 'OPTIONS_FOUND' or not options:
        return 'No independently validated single-class alternative is available. No change was made.', None, False
    criterion = 'total_quality'
    used_ai = False
    # Explicit preferences take precedence over guesses (and save an API call).
    q = str(request or '').lower()
    if re.search(r'\b(faculty|teacher)\b.*\b(gaps?|breaks?)\b|\b(gaps?|breaks?)\b.*\b(faculty|teacher)\b', q):
        criterion = 'faculty_gaps'
    elif re.search(r'\b(section|student)\b.*\b(gaps?|breaks?)\b|\b(gaps?|breaks?)\b.*\b(section|student)\b', q):
        criterion = 'section_gaps'
    elif re.search(r'\b(soft.avoid|avoid late|late period|policy penalty)\b', q):
        criterion = 'soft_avoid_periods'
    elif llm_enabled:
        try:
            if planner is None:
                from timetable_cloud_ai import choose_scheduling_priority
                planner = choose_scheduling_priority
            ai_choice = planner(request, model=model)
            if ai_choice in ('total_quality', 'faculty_gaps', 'section_gaps', 'soft_avoid_periods'):
                criterion = ai_choice
                used_ai = True
        except (OSError, ValueError, KeyError, TypeError):
            pass
    winner = min(options, key=lambda o: _score_option(o, criterion))
    delta = winner.get('delta') or {}
    reason = (f'faculty gaps {int(delta.get("faculty_gaps", 0)):+d}, '
              f'section gaps {int(delta.get("section_gaps", 0)):+d}, '
              f'soft-avoid periods {int(delta.get("soft_avoid_periods", 0)):+d}')
    explain = {
        'total_quality': 'the lowest verified overall scheduling penalty',
        'faculty_gaps': 'the fewest additional faculty gap periods',
        'section_gaps': 'the fewest additional section gap periods',
        'soft_avoid_periods': 'the fewest additional soft-avoid teaching periods',
    }[criterion]
    name = 'Groq-guided recommendation' if used_ai else 'Verified scheduling recommendation'
    text = (f'**{name}: {winner["slot"]} · Room {winner["room_id"]}.**\n\n'
            f'This is preferred among the {len(options)} displayed independently validated '
            f'options for {explain}. Measured changes relative to the current timetable: {reason}.\n\n'
            '**Review before approval. No class has been moved.**')
    if proposal.get('examined_valid_options', 0) > len(options):
        text += ('\n\n*Ranking covers only the displayed candidates; '
                 'additional validated alternatives may exist.*')
    return text, winner['slot_id'], used_ai
