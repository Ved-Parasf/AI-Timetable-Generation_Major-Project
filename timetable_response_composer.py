"""One-response Groq wording, with verified Python facts as the sole evidence.

The model never provides timetable data or permission to reschedule. When a
rewrite changes an identifier or number, the original verified reply wins.
"""
from __future__ import annotations
import re
from timetable_ai_upgrade import eligible_for_analysis, NO_EVIDENCE_PHRASES

_NUMBER = re.compile(r'(?<![A-Za-z])\d+(?:\.\d+)?%?')
_ID = re.compile(r'\b(?:[A-Z]{2,7}\d{2,6}[A-Z]?|RN\s*\d{1,5}|P\d{1,2})\b',re.I)


def _facts(text):
    return (set(_NUMBER.findall(text)), {re.sub(r'\s+','',t).upper() for t in _ID.findall(text)})


def one_groq_answer(question: str, verified: str, *, model=None, explainer=None, all_questions=False):
    """Return a SINGLE grounded answer; fall back on the verified one.

    Uses Groq to make complex answers clearer, not to invent new facts. The
    original can always be inspected via independent reports, never rewritten
    for safety-sensitive conflict/rescheduling operations.
    """
    source = (verified or '').strip()
    q = (question or '').strip()
    # A clarification is not evidence. Groq must not invent the missing
    # session, teacher, free slot, clash or coordinator approval.
    if not q or not source:
        return verified, False
    if not all_questions and not eligible_for_analysis(q, source):
        return verified, False
    if all_questions:
        lowered = source.lower()
        if (any(fragment in lowered for fragment in NO_EVIDENCE_PHRASES)
                or ('which existing session' in lowered)
                or ('which code do you mean' in lowered)
                or (source.endswith('?') and len(source) < 300)
                or len(source) > 7800):
            return verified, False
        # Rescheduling must remain on the validated, coordinator-controlled
        # workflow; the user does not want model-selected or invented moves.
        if re.search(r'\b(?:move|shift|reschedul\w*|swap|confirm|approve|cancel|'
                     r'can .*be scheduled|can .*be moved)\b', q, re.I):
            return verified, False
    from timetable_cloud_ai import explain_verified, answer_verified, DEFAULT_MODEL
    explain = explainer or (answer_verified if all_questions else explain_verified)
    wording, error = explain(
        'Write ONE direct answer to the question using ONLY the verified evidence. '
        'Do not give a second separate Python answer or label another response. '
        'Keep ALL exact names, course IDs, periods, counts, metrics and relevant caveats. '
        'Do not infer a new free slot, conflict, reason, recommendation or action. '
        'If the evidence cannot answer an aspect, say that it is unknown. '
        'Question: '+question[:350],
        'EXACT VERIFIED TOOL RESPONSE:\n'+verified[:7600] if all_questions else 'EXACT VERIFIED TOOL RESPONSE:\n'+verified[:3000],
        model=model or DEFAULT_MODEL)
    if error or not isinstance(wording,str) or not wording.strip():
        return verified,False
    wording=wording.strip()
    numbers_before, ids_before = _facts(verified)
    numbers_after, ids_after = _facts(wording)
    if (not numbers_after.issubset(numbers_before)
            or not ids_after.issubset(ids_before)
            or len(wording) < 25 or len(wording)>3100):
        return verified,False
    # Do not allow dropping numeric evidence during a quality comparison.
    if len(numbers_before) >= 3 and not all_questions and not numbers_before.issubset(numbers_after):
        return verified,False
    # Preserve every name in compact teacher lists; do not let a fluent
    # summary omit a teacher from a question asking who teaches a course.
    if all_questions and re.search(r'\bwho\s+teaches?\b|\bteachers?\b.*\bwhen\b', q, re.I):
        teacher_bullets = re.findall(r'^\s*[-*]\s*\*{0,2}([^*:\n—]{3,75})\*{0,2}\s*(?:—|:)', source, re.M)
        if 1 <= len(teacher_bullets) <= 12 and any(name.strip().lower() not in wording.lower() for name in teacher_bullets):
            return verified, False
    return wording,True
