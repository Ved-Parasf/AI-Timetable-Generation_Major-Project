"""Conversation-to-verified-one-session-move bridge.

No model-generated placement or natural-language message changes stored data.
Every suggestion is produced by the existing independent rescheduler. A separate
explicit confirmation button is required to publish a new database version.
"""
from __future__ import annotations

import re
from collections import defaultdict

from timetable_assistant import (
    _belongs, _group_match, match_course_reference, match_teacher,
    parse_day, parse_period, parse_clock_minutes, _slot_for_clock_time, norm,
)
from timetable_rescheduling import propose_moves, schedule_hash

MOVE_WORD = re.compile(r'\b(?:move|moving|shift|shifting|reschedul\w*|relocat\w*|rearrang\w*|swap|transfer|postpone|bring forward|change timing|change the time)\b', re.I)
SCHEDULE_CLASS = re.compile(r'\b(?:schedule|place|put)\s+(?:a|the|this|my|an)?\s*(?:class|session|course)\b', re.I)
SLOT_WORD = re.compile(r'\b(?:free|available|valid|legal|alternative|possible|suitable)\s+(?:\w+\s+){0,3}(?:slots?|periods?|times?)\b', re.I)
CANCEL_WORD = re.compile(r'^\s*(?:cancel|never mind|nevermind|stop|discard)(?:\s+(?:it|move|request))?[.!]?\s*$', re.I)
FOLLOWUP_WORD = re.compile(r'^\s*(?:for|in|on|at|to|into|period|p\d+|choose|select|the|that|same|what about|how about)\b', re.I)


def _sessions(rows):
    out = defaultdict(list)
    for row in rows:
        sid = norm(row.get('session_id'))
        if sid:
            out[sid].append(row)
    return out


def session_label(session_rows):
    rows = sorted(session_rows, key=lambda r: int(float(r['period'])))
    first, last = rows[0], rows[-1]
    periods = f"P{first['period']}" + (f"–P{last['period']}" if len(rows) > 1 else '')
    return (f"{first['course_id']} · {first['group_id']} · {first['kind']} · "
            f"{first['faculty_name']} · {first['day']} {periods}")


def matching_sessions(question, rows, data, *, source_description=False):
    """Filter real *session IDs* by explicit source entities, never by guesses."""
    sessions = _sessions(rows)
    catalog = {norm(c.get('course_id')): c for c in data.get('Courses', [])}
    course, ambiguous = match_course_reference(question, catalog, rows)
    if ambiguous:
        return [], 'Which course code do you mean: ' + ', '.join(ambiguous) + '?'
    groups = {norm(g.get('group_id')): g for g in data.get('Student_Groups', [])}
    group = _group_match(question, groups, rows)
    teacher, _ = match_teacher(question, {norm(f.get('faculty_name')) for f in data.get('Faculty', [])})
    quoted_sid = next((sid for sid in sessions if re.search(r'(?<![A-Za-z0-9_])' + re.escape(sid) + r'(?![A-Za-z0-9_])', question, re.I)), None)
    if not (course or group or teacher or quoted_sid):
        return [], 'Tell me the **course and section** (or the exact session ID) you want to shift.'
    practical = bool(re.search(r'\b(labs?|practicals?)\b', question, re.I))
    theory = bool(re.search(r'\b(theory|lecture)\b', question, re.I))
    # Only explicit CURRENT/FROM phrases identify the *source* period. A period
    # after "move ... to" is instead a requested destination.
    from_part = re.search(r'\b(?:from|currently\s+(?:on|at)|originally\s+(?:on|at))\b(.+?)(?:\bto\b|$)', question, re.I)
    old_day = parse_day(from_part.group(1)) if from_part else None
    old_period = parse_period(from_part.group(1)) if from_part else None
    # Natural requests may name the source directly: "move BCL203 Monday P4
    # to Friday P2". Only text BEFORE the post-action destination is used
    # to identify the current session, never the proposed destination slot.
    if not from_part:
        action = MOVE_WORD.search(question)
        if action:
            after_action = question[action.end():]
            destination_marker = re.search(r'\b(?:to|into)\b', after_action, re.I)
            source_text = (after_action[:destination_marker.start()]
                           if destination_marker else after_action)
            if parse_day(source_text):
                old_day = parse_day(source_text)
                old_period = parse_period(source_text)
    # After "I want to reschedule a class", the coordinator may reply with
    # only a class description plus its ORIGINAL day and period. There is no
    # destination in that follow-up, so use the explicit day/period to select
    # the source session rather than listing unrelated same-course sessions.
    if source_description and not (MOVE_WORD.search(question) or SCHEDULE_CLASS.search(question)):
        old_day = old_day or parse_day(question)
        if old_period is None:
            old_period = parse_period(question)
    found = []
    for sid, grouped in sessions.items():
        r = grouped[0]
        if quoted_sid and sid != quoted_sid:
            continue
        if course and norm(r.get('course_id')) != course:
            continue
        if group and not _belongs(r, group, groups):
            continue
        if teacher and norm(r.get('faculty_name')) != teacher:
            continue
        if practical and norm(r.get('kind')).upper() != 'PRACTICAL':
            continue
        if theory and norm(r.get('kind')).upper() != 'THEORY':
            continue
        if old_day and norm(r.get('day')).upper() != old_day:
            continue
        if old_period is not None and not any(int(float(x['period'])) == old_period for x in grouped):
            continue
        found.append(sid)
    return sorted(found, key=lambda sid: session_label(sessions[sid])), None


def requested_destination(question, data):
    """Find only a workbook-defined slot. Never infer a desired time from an LLM."""
    q = question
    # Source-only descriptions should not be mistaken for destinations.
    if re.search(r'\bfrom\b', q, re.I) and not re.search(r'\b(?:to|into)\b', q, re.I):
        return None
    # Distinguish the existing class time from the proposed new slot. A
    # "want to move" preamble is NOT a destination, and neither is "on Monday".
    action = MOVE_WORD.search(q)
    if action:
        after_action = q[action.end():]
        marker = re.search(r'\b(?:to|into)\b\s+(.+)', after_action, re.I)
        if not marker:
            return None
        q = marker.group(1)
    elif re.search(r'\bfrom\b', q, re.I):
        match = re.search(r'\b(?:to|into)\b\s+(.+)', q, re.I)
        if match:
            q = match.group(1)
    day = parse_day(q)
    period = parse_period(q)
    clock = parse_clock_minutes(q)
    if not day:
        return None
    if period is None and clock is not None:
        chosen = _slot_for_clock_time(data, day, clock)
        if chosen:
            period = int(float(chosen['period']))
    if period is None:
        return None
    for sl in data.get('Time_Slots', []):
        if norm(sl.get('day')).upper() == day and int(float(sl['period'])) == period:
            # If user supplied both clock and period, refuse a contradictory pair.
            if clock is not None:
                selected = _slot_for_clock_time(data, day, clock)
                if not selected or int(float(selected['period'])) != period:
                    return None
            return norm(sl['slot_id'])
    return None


def requested_destination_day(question):
    """Read a *destination* weekday without guessing the starting period.

    "Move Monday's class to Friday" must never be interpreted as a move
    *from* Friday; we retain Friday as the desired destination filter.
    """
    question = str(question or '')
    start = MOVE_WORD.search(question)
    remainder = question[start.end():] if start else question
    marker = re.search(r'\b(?:to|into)\s+(.+)', remainder, re.I)
    return parse_day(marker.group(1)) if marker else None


def _describe_options(proposal):
    if proposal.get('status') != 'OPTIONS_FOUND':
        reason = '; '.join((proposal.get('reasons') or proposal.get('requested_reasons', []))[:2])
        return ('**No validated single-class alternative found.** ' + reason +
                ' Other classes may need to move; nothing has changed.')
    lines = []
    for index, option in enumerate(proposal['options'], 1):
        changes = option['delta']
        lines.append(f"{index}. **{option['slot']}** · Room {option['room_id']} "
                     f"(faculty gaps {changes['faculty_gaps']:+d}, "
                     f"section gaps {changes['section_gaps']:+d})")
    intro = (f"**{proposal['course_id']} / {proposal['group_id']}** — "
             f"currently {proposal['current_slot']}, Room {proposal['current_room']}. "
             f"Found {proposal['examined_valid_options']} independently validated alternative start slots.\n\n")
    if proposal.get('requested_reasons'):
        intro += ('**Requested slot not permitted:** ' + '; '.join(proposal['requested_reasons'][:2]) + '\n\n')
    return intro + '\n'.join(lines) + ('\n\nChoose a slot below, or reply with a day and period. '
                                      '**No change has been made.**')


def prepare_for_session(session_id, rows, data, *, include_mentoring=True, extra_labs=0,
                        requested_slot=None, desired_day=None, max_options=10):
    sessions = _sessions(rows)
    if session_id not in sessions:
        raise ValueError('The selected teaching session is not in the current timetable')
    # For a requested weekday, examine ALL defined starts before filtering.
    # A top-10 overall shortlist could otherwise silently hide safe Friday
    # placements behind higher-scoring Tuesday placements.
    count = max(max_options, len(data.get('Time_Slots', []))) if desired_day else max_options
    proposal = propose_moves(data, rows, session_id, requested_slot_id=requested_slot,
                             include_mentoring=include_mentoring,
                             extra_simulated_labs=extra_labs,
                             maximum_options=count)
    if desired_day and proposal.get('status') == 'OPTIONS_FOUND':
        proposal = dict(proposal)
        all_count = proposal.get('examined_valid_options', 0)
        proposal['options'] = [opt for opt in proposal['options']
                               if str(opt.get('slot_id', '')).upper().startswith(desired_day + '_')]
        proposal['examined_valid_options_all_days'] = all_count
        proposal['examined_valid_options'] = len(proposal['options'])
        proposal['desired_day'] = desired_day
        if proposal['options']:
            proposal['recommended_index'] = min(
                range(len(proposal['options'])),
                key=lambda index: (proposal['options'][index]['rank_cost'],
                                   proposal['options'][index]['slot_id']))
        else:
            proposal['status'] = 'NO_VALID_SINGLE_MOVE'
            proposal['reasons'] = [
                f'No independently validated placement was found on {desired_day}. '
                'You can request another destination day; no timetable change was made.']
    state = dict(stage='options', base_hash=schedule_hash_from_rows(rows),
                 session_id=session_id, proposal=proposal,
                 chosen_slot=requested_slot if any(
                     o['slot_id'] == requested_slot for o in proposal.get('options', [])) else
                     (proposal['options'][proposal['recommended_index']]['slot_id']
                      if proposal.get('options') and proposal.get('recommended_index') is not None else None))
    return _describe_options(proposal), state


def schedule_hash_from_rows(rows):
    from timetable_rescheduling import csv_bytes
    return schedule_hash(csv_bytes(rows))


def is_move_request(question, *, llm_enabled=False, model='openai/gpt-oss-120b'):
    from timetable_timora_intelligence import normalize_task_text
    question = normalize_task_text(question)
    if re.search(r'^\s*(?:how|what\s+(?:is|are|does)|why)\b', question, re.I):
        return False
    if re.search(r'^\s*(?:can|could|would|is it possible)\b', question, re.I) and re.search(
            r'\b(?:be scheduled|be placed|be moved|be shifted|explain.*conflict)\b', question, re.I):
        return False
    if (MOVE_WORD.search(question) or SCHEDULE_CLASS.search(question)
            or (SLOT_WORD.search(question) and re.search(
                r'\b(?:class|session|shift|move|reschedul)\b', question, re.I))):
        return True
    # Only ask Groq for *plausible* task instructions, not factual questions.
    # Groq returns an operation label; Python must resolve exact identifiers.
    likely_action = (re.search(r'\b(?:please|want|need|could you|can you|would like|help me|adjust|different day|different time)\b',
                               question, re.I)
                     and re.search(r'\b(?:class|lecture|course|period|slot|[A-Z]{2,6}\s*\d{2,4})\b',
                                   question, re.I))
    if not (llm_enabled and likely_action):
        return False
    try:
        from timetable_cloud_ai import select_timetable_tool, classify_question
        selected = select_timetable_tool(question, model=model)
        if selected == 'request_class_move_preview':
            return True
        if selected == 'answer_timetable_question':
            return False
        # JSON-classification fallback for providers that do not return a
        # native function call. It can still only request a preview.
        plan = classify_question(question, model=model)
        return bool(plan and plan.get('intent') == 'move_guidance')
    except (OSError, ValueError, TypeError, KeyError):
        return False


def route_move_message(question, rows, data, state=None, *, include_mentoring=True,
                       extra_labs=0, llm_enabled=False,
                       model='openai/gpt-oss-120b'):
    """Return (handled, reply, state). Chat may *prepare* but never commit."""
    from timetable_timora_intelligence import normalize_task_text
    from timetable_spelling import normalize_question
    question = normalize_task_text((question or '').strip())
    spelling = normalize_question(question, data, rows)
    if spelling.clarification:
        return True, spelling.clarification + ' No timetable change has been made.', state
    question = spelling.question
    base_hash = schedule_hash_from_rows(rows)
    if state and state.get('base_hash') != base_hash:
        state = None
    if state and CANCEL_WORD.fullmatch(question):
        return True, 'Class-move preview cancelled. The timetable is unchanged.', None
    # An unfinished move is retained for follow-ups, but must not consume an
    # independent teacher-analysis or room-availability question. In particular
    # "Which teachers have more than 20 teaching periods?" is NOT a move.
    if state and not is_move_request(question) and (
        (re.search(r'\b(?:teachers?|faculty|professors?)\b', question, re.I)
         and re.search(r'\b(?:workload|load|hours?|periods?|classes|sessions|most|least|'
                       r'highest|lowest|more than|fewer than|compare)\b', question, re.I))
        or (re.search(r'\b(?:rooms?|classrooms?|labs?|laboratories)\b', question, re.I)
            and re.search(r'\b(?:free|available|vacant|occupied)\b', question, re.I))
    ):
        return False, None, state
    if state and state.get('stage') == 'options':
        if re.search(r'\b(?:best|recommended|recommend|preferred|most suitable|which alternative|choose for me)\b',
                     question, re.I):
            from timetable_timora_intelligence import verified_slot_advice
            advice, slot_id, _ = verified_slot_advice(
                state['proposal'], question, llm_enabled=llm_enabled, model=model)
            if slot_id:
                return True, advice + '\n\nSelect this option below and approve it to publish.', dict(state, chosen_slot=slot_id)
            return True, advice, state
        slot_id = requested_destination(question, data)
        is_followup = bool(FOLLOWUP_WORD.search(question) or
                           re.search(r'\b(?:choose|select|use|this slot|that slot|confirm|apply|approve)\b', question, re.I))
        if slot_id and (is_followup or MOVE_WORD.search(question) or
                        re.fullmatch(r'\s*(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\s+(?:p(?:eriod)?\s*)?\d+\s*[.!]?\s*',
                                     question, re.I)):
            option = next((x for x in state['proposal'].get('options', []) if x['slot_id'] == slot_id), None)
            if option:
                state = dict(state, chosen_slot=slot_id)
                return True, (f"Selected **{option['slot']}**, Room {option['room_id']}. "
                              'Review the change below and click **Confirm and update timetable**. '
                              'Nothing has been changed yet.'), state
            return True, (f'**{slot_id} is not one of the independently validated alternatives.** '
                          'Select one of the listed free slots instead. No change was made.'), state
        if re.search(r'^\s*(?:confirm|apply|approve)(?:\s+(?:it|move|change))?[.!]?\s*$', question, re.I):
            return True, 'For safety, use the **Confirm and update timetable** button below. Typing approval does not publish a change.', state
    # In a multi-turn move the coordinator may give the class description in
    # the NEXT message, or ask for the scheduled days before choosing one.
    # These messages are not new independent questions and must retain state.
    if state and state.get('stage') in ('need_class', 'select_session'):
        from timetable_chat_queries import course_schedule_answer
        from timetable_assistant import _group_match
        raw_query = state.get('query', '')
        combined = (raw_query + ' ' + question).strip()
        catalog = {norm(c.get('course_id')): c for c in data.get('Courses', [])}
        course, ambiguous = match_course_reference(combined, catalog, rows)
        if ambiguous:
            return True, 'Which course code do you mean: ' + ', '.join(ambiguous) + '?', state
        groups = {norm(g.get('group_id')): g for g in data.get('Student_Groups', [])}
        group = _group_match(combined, groups, rows)
        if (course and state['stage'] == 'select_session' and re.search(
                r'\b(?:what days?|which days?|give days|days when|when is|when are|'
                r'list (?:the )?(?:days|schedule)|show (?:the )?schedule|scheduled on)\b',
                question, re.I)):
            # An inquiry does NOT reset the move target or choose a new slot.
            kind = 'THEORY' if re.search(r'\btheory\b', raw_query, re.I) else (
                'PRACTICAL' if re.search(r'\b(?:lab|practical)\b', raw_query, re.I) else None)
            return True, (course_schedule_answer(rows, course, group=group, kind=kind)
                          + '\n\nTo continue shifting, tell me the **current day** '
                            'or select the exact session below. Nothing has changed.'), state
        if state['stage'] == 'need_class':
            if not course and not _group_match(question, groups, rows):
                return False, None, state
            matches, error = matching_sessions(question, rows, data, source_description=True)
            if error:
                return True, error, state
        else:
            # Restrict only to the ALREADY identified course/section sessions.
            # "for Monday" names a SOURCE day, not a destination slot.
            matches = list(state['matches'])
            current_day = parse_day(question)
            current_period = parse_period(question)
            if current_day:
                matches = [sid for sid in matches if norm(_sessions(rows)[sid][0].get('day')).upper() == current_day]
            if current_period is not None:
                matches = [sid for sid in matches if any(
                    int(float(r.get('period', 0))) == current_period for r in _sessions(rows)[sid])]
            if re.search(r'\btheory\b', question, re.I):
                matches = [sid for sid in matches if norm(_sessions(rows)[sid][0].get('kind')).upper() == 'THEORY']
            if re.search(r'\b(?:practical|lab)\b', question, re.I):
                matches = [sid for sid in matches if norm(_sessions(rows)[sid][0].get('kind')).upper() == 'PRACTICAL']
            # Do not interpret a completely unrelated question as move selection.
            relevant = (current_day or current_period is not None or
                        FOLLOWUP_WORD.search(question) or
                        any(norm(x).lower() in question.lower() for x in state['matches']) or
                        (course and course.lower() in question.lower()))
            if not relevant:
                return False, None, state
        if not matches:
            return True, ('No matching current class on that day/period. '
                          'The previous timetable is unchanged; choose another day or session.'), state
        if len(matches) == 1:
            reply, revised = prepare_for_session(
                matches[0], rows, data, include_mentoring=include_mentoring,
                extra_labs=extra_labs,
                requested_slot=(requested_destination(question, data) if is_move_request(question) else
                    (requested_destination(state.get('query',''), data)
                     if re.search(r'\b(?:to|into)\b', state.get('query',''), re.I) else None)),
                desired_day=requested_destination_day(question) or requested_destination_day(state.get('query', '')))
            return True, reply, revised
        # Always show the actual day/period choices, not a repeated vague prompt.
        choices = _sessions(rows)
        preview = '\n'.join(f'- {session_label(choices[sid])} · session `{sid}`'
                            for sid in matches[:20])
        new_state = dict(stage='select_session', query=combined,
                         base_hash=base_hash, matches=matches)
        return True, (f'**{len(matches)} current sessions match.** '
                      'Which one should be shifted?\n\n' + preview +
                      '\n\nTell me the original day/period or use the session selector.'), new_state
    if not is_move_request(question, llm_enabled=llm_enabled, model=model):
        return False, None, state
    matches, error = matching_sessions(question, rows, data)
    if error:
        return True, error, (dict(stage='need_class', query=question,
                                  base_hash=base_hash) if is_move_request(question) else None)
    if not matches:
        return True, 'No matching scheduled session found. Check the course, section or lab group.', None
    if len(matches) > 1:
        choices = _sessions(rows)
        preview = '\n'.join(f'- {session_label(choices[sid])}' for sid in matches[:6])
        return True, (f'Found **{len(matches)}** matching sessions. Choose the one to move below.\n\n'
                      + preview + ('\n- …' if len(matches) > 6 else '') +
                      '\n\nNo change has been made.'), dict(stage='select_session', query=question,
                                                       base_hash=base_hash, matches=matches)
    reply, new_state = prepare_for_session(matches[0], rows, data,
        include_mentoring=include_mentoring, extra_labs=extra_labs,
        requested_slot=requested_destination(question, data),
        desired_day=requested_destination_day(question))
    return True, reply, new_state
