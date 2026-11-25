"""Source-grounded timetable agent with optional cloud AI intent routing.

Only Python answers factual questions. Groq can classify an unfamiliar
question's intent but NEVER supplies teacher availability, rooms, schedules,
conflict facts, or a second contradictory reply. No LLM action edits CP-SAT.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from timetable_assistant import (response as grounded_response, match_teacher,
                                 parse_day, parse_period, parse_clock_minutes,
                                 match_course_reference, norm)

from timetable_cloud_ai import DEFAULT_MODEL, classify_question, explain_verified


def query_local_qwen(question: str, evidence: str, *, model: str = DEFAULT_MODEL,
                     timeout_seconds: int = 16) -> tuple[str | None, str]:
    """Backward-compatible helper for verified cloud explanations.

    No action or timetable fact comes from the remote model. This routine is
    intentionally not called by normal factual question-answering.
    """
    text, error = explain_verified(question, evidence, model=model)
    return text, ('AI explanation of verified diagnostics' if text else error or 'Cloud unavailable')


def _classify_local_intent(question: str, *, model: str = DEFAULT_MODEL,
                           timeout_seconds: int = 12) -> str:
    plan = classify_question(question, model=model)
    allowed = ('teacher_availability', 'teacher_sections', 'course_teachers')
    return plan['intent'] if plan and plan['intent'] in allowed else 'unknown'



def _try_grounded_intent_rewrite(question, rows, data, report, model):
    """Use an LLM *classification* only when all query entities are grounded.

    The reconstructed query reuses only entities found in the user's original
    message. The output always comes from grounded_response, never the model.
    """
    names = {str(r.get('faculty_name') or '') for r in data.get('Faculty', [])}
    names.update(str(r.get('faculty_name') or '') for r in rows)
    teacher, _ = match_teacher(question, names)
    course_catalog = {norm(r.get('course_id')): r for r in data.get('Courses', [])}
    course, ambiguous = match_course_reference(question, course_catalog, rows)
    day = parse_day(question)
    period, clock = parse_period(question), parse_clock_minutes(question)
    # Do not call a local LLM when no teacher or course from original text
    # could possibly ground a supported answer.
    if not teacher and (not course or ambiguous):
        return None
    intent = _classify_local_intent(question, model=model)
    if intent == 'teacher_availability' and teacher and day:
        # Even the rewritten question retains the original day/time; Python
        # resolves the actual period and checks the workbook.
        rewritten = ('Is ' + teacher + ' available? ' + question) if (period is not None or clock is not None)                     else ('When is ' + teacher + ' available on ' + {
                        'MON':'Monday','TUE':'Tuesday','WED':'Wednesday','THU':'Thursday',
                        'FRI':'Friday','SAT':'Saturday','SUN':'Sunday'}[day] + '?')
        return grounded_response(rewritten, rows, data, report).strip()
    if intent == 'teacher_sections' and teacher and period is None and clock is None:
        day_phrase = (' on ' + {'MON':'Monday','TUE':'Tuesday','WED':'Wednesday','THU':'Thursday',
                                  'FRI':'Friday','SAT':'Saturday','SUN':'Sunday'}[day]) if day else ''
        return grounded_response('Which sections does ' + teacher + ' teach' + day_phrase + '?',
                                 rows, data, report).strip()
    if intent == 'course_teachers' and course and not ambiguous and period is None and clock is None:
        return grounded_response('Who teaches ' + course + '?', rows, data, report).strip()
    return None


def _grounded_move_guidance(question, rows, data):
    """Explain what session the coordinator must select before a class move.

    No editing is done here. The existing change-request form and independent
    validation continue to be the only path to an approved revised schedule.
    """
    catalog = {norm(r.get('course_id')):r for r in data.get('Courses', [])}
    course, ambiguous = match_course_reference(question, catalog, rows)
    if ambiguous:
        return ('Which course do you mean: ' + ', '.join(sorted(ambiguous))
                + '? No change has been made.')
    if not course:
        return ('**No timetable change has been made.** Select the exact scheduled '
                'session and destination slot in **Timora → Prepare a '
                'class-move request**. The coordinator must validate and approve it.')
    practical = bool(re.search(r'\b(lab|practical)\b', question, re.IGNORECASE))
    records = [r for r in rows if norm(r.get('course_id')) == course
               and (not practical or norm(r.get('kind')).upper() == 'PRACTICAL')]
    groups = sorted({norm(r.get('group_id')) for r in records if norm(r.get('group_id'))})
    sessions = {norm(r.get('session_id')) for r in records if norm(r.get('session_id'))}
    if not records:
        return (f'No {"practical " if practical else ""}session for **{course}** '
                'is present in the selected timetable. No change has been made.')
    if len(groups) > 1:
        examples = ', '.join(groups[:7]) + (f', and {len(groups)-7} more' if len(groups)>7 else '')
        detail = (f'**{course} has {len(sessions)} matching scheduled session(s)** across '
                  f'{len(groups)} groups/batches. Please select the specific one: '
                  f'{examples}. ')
    else:
        detail = f'**{course} — {groups[0]}**. '
    return (detail + 'A weekday and period alone do not prove that a move is safe: '
            'the selected session, its complete duration, teacher, group, room and '
            'institutional constraints must be checked. Use **Timora → '
            'Prepare a class-move request**, then review the validated proposal in '
            '**Coordinator workflows**. **No change has been made.**')


def answer(question, rows, data, validation_report=None, *, llm_enabled=False,
           model=DEFAULT_MODEL, recent_diagnosis=None, conversation=()):
    """Get grounded facts first; optional LLM cannot override source of truth."""
    question = (question or '').strip()
    if not question:
        return 'Enter a question about the current timetable.'
    # V15: normalize probable spelling errors against the active workbook and
    # selected schedule BEFORE any routing/LLM calls. Do not guess ambiguous
    # teacher/course identities or change any underlying timetable data.
    from timetable_spelling import normalize_question
    spelling = normalize_question(question, data, rows)
    if spelling.clarification:
        return spelling.clarification
    question = spelling.question
    from timetable_chat_intents import answer_high_confidence
    direct = answer_high_confidence(question, rows, data)
    if direct is not None:
        return direct
    from timetable_timora_intelligence import all_available_faculty_answer
    all_free = all_available_faculty_answer(question, rows, data)
    if all_free is not None:
        return all_free
    lowered = question.lower()
    # V14 single conversational entry point: resolve meaning, state and trusted
    # tools BEFORE the older phrase-based parser can return irrelevant dumps.
    # The legacy parser remains a fallback for its specialised diagnostics.
    if not (recent_diagnosis and any(term in lowered for term in
            ('last move', 'last request', 'my request', 'move rejected',
             'request rejected', 'rejection reason'))):
        from timetable_dialogue import respond
        conversational = respond(question, rows, data, validation_report,
                                  conversation=conversation, llm_enabled=llm_enabled,
                                  model=model)
        if conversational is not None:
            from timetable_timora_intelligence import retry_incorrect_clarification
            improved, used_groq = retry_incorrect_clarification(
                question, conversational, rows, data, validation_report,
                conversation=conversation, llm_enabled=llm_enabled, model=model)
            if improved is not None:
                return (improved + '\n\n*Groq interpreted · Python verified*'
                        if used_groq else improved)
            return conversational
    # Use the intent/tools layer directly for broad room searches. The older
    # question parser requires a period and would prematurely return a generic
    # clarification even when the user clearly asks about an entire day.
    from timetable_nl_router import _local_intent, answer_structured
    if (_local_intent(question) == 'room_availability'
            and parse_period(question) is None
            and parse_clock_minutes(question) is None):
        routed = answer_structured(question, rows, data, validation_report,
                                   conversation=conversation, llm_enabled=llm_enabled,
                                   model=model)
        if routed:
            return routed
    move_intent = any(x in lowered for x in ('move ', 'shift ', 'reschedul', 'swap ', 'change class'))
    if move_intent:
        evidence = _grounded_move_guidance(question, rows, data)
    elif (recent_diagnosis and any(x in lowered for x in ('last move', 'last request', 'my request', 'move rejected', 'request rejected', 'rejection reason'))):
        evidence = ('Most recent inspected move:\n' + recent_diagnosis[:3500] +
                    '\nNo change occurs until coordinator approval.')
    else:
        evidence = grounded_response(question, rows, data, validation_report).strip()
    # Exactly one grounded answer per question. The language model may help
    # route an unfamiliar *intent*, but its free-text output is never shown as
    # a competing availability answer.
    if llm_enabled and not move_intent and not recent_diagnosis:
        unsupported = ('I could not identify a supported timetable query' in evidence
                       or 'No matching scheduled session was found' in evidence)
        # A teacher/course mention used to trigger an unhelpful entire-session
        # dump even for open-ended "which cohorts"/"who is responsible" queries.
        # Allow optional cloud intent classification, but only if the question
        # is not explicitly asking for a schedule/day/time/session listing.
        generic_dump = ('matching session(s)' in evidence
                        and bool(re.search(r'\b(who|which|what|responsible|cohorts?|assigned)\b', lowered))
                        and not bool(re.search(r'\b(timetable|schedule|periods?|on monday|on tuesday|on wednesday|on thursday|on friday)\b', lowered)))
        if unsupported or generic_dump:
            rerouted = _try_grounded_intent_rewrite(question, rows, data,
                                                    validation_report, model)
            if rerouted and 'I could not identify a supported timetable query' not in rerouted:
                return rerouted
    # Semantic routing is a second, data-grounded interpretation layer.
    # Keep established deterministic answers intact when Qwen is off; use
    # structured tools for unsupported phrasing, short contextual followups,
    # and model-assisted interpretation when explicitly enabled.
    from timetable_nl_router import answer_structured, SHORT_FOLLOWUP
    unsupported = ('I could not identify a supported timetable query' in evidence
                   or 'No matching scheduled session was found' in evidence)
    from timetable_nl_router import _local_intent
    useful_intents = {'teacher_sections','teacher_courses','course_teachers',
                      'teacher_availability','common_availability','course_sections',
                      'section_courses','room_availability'}
    requested_schedule = bool(re.search(
        r'\b(timetable|schedule|sessions?|classes|lectures?|when does .* teach|daily|weekly|all periods)\b',
        lowered))
    generic_dump = ('matching session(s)' in evidence
                    and (_local_intent(question) in useful_intents or
                         (llm_enabled and not requested_schedule))
                    and not re.search(r'\b(timetable|schedule|show all classes|daily|weekly|all sessions)\b', lowered))
    followup = bool(conversation and SHORT_FOLLOWUP.search(question))
    if (unsupported or generic_dump or followup) and not (recent_diagnosis and evidence.startswith("Most recent inspected move:")):
        routed = answer_structured(question, rows, data, validation_report,
                                   conversation=conversation, llm_enabled=llm_enabled,
                                   model=model)
        if routed and not (unsupported and 'No matching scheduled session' in routed):
            return routed
    if unsupported and 'I could not identify a supported timetable query' in evidence:
        return ('I can help you check timetable assignments, teacher or section free time, '
                'classrooms, teaching workload, policies and proposed class moves. '
                'Could you tell me which teacher, course, section or room you want to analyze? '
                'You can ask in your own words.')
    return evidence
