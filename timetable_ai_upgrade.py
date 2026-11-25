"""Optional evidence-grounded Groq analysis of COMPLEX timetable questions.

This does not generate, relocate or approve a teaching session. The verified
Python answer is displayed unchanged first. Groq's separately labeled analysis
may explain evidence but is never treated as a source of schedule facts.
"""
from __future__ import annotations

import json
import re

# Avoid using an LLM for simple factual yes/no or name lookups. The model is
# called for explicit interpretation, comparison or recommendation requests.
ANALYSIS_WORDS = re.compile(
    r"\b(?:why|explain|reason|because|compare|comparison|difference|versus|vs|"
    r"analy[sz]e|analysis|evaluat\w*|recommend\w*|suggest\w*|trade[ -]?offs?|"
    r"improv\w*|optimi[sz]\w*|fairness|fairer|better|worse|impact|what if|"
    r"consequences?|advantages?|disadvantages?)\b",
    re.IGNORECASE,
)

# Do not present a simulated explanation as a substitute for missing facts or
# a requested clarification. These are legacy assistant phrases, not user data.
NO_EVIDENCE_PHRASES = (
    'i could not identify a supported timetable query',
    'which teacher should i check', 'which section should i check',
    'which student section', 'which course do you mean',
    'no matching scheduled session was found',
    'please upload a timetable workbook',
    'no answer available',
    'which teacher, section or course timetable',
)


def eligible_for_analysis(question: str, verified_answer: str) -> bool:
    """True when an analytical request has a useful source-grounded answer."""
    question = (question or '').strip()
    answer = (verified_answer or '').strip()
    if not question or not answer or not ANALYSIS_WORDS.search(question):
        return False
    # A generic prose explainer previously invented teacher and student clashes
    # when the only checked issue was a flexible lunch requirement. Move/conflict
    # questions must use the evidence-locked Check Conflicts pathway instead.
    if re.search(r'\b(?:move|shift|reschedul\w*|clash\w*|conflict\w*|overlap\w*|lunch|'
                 r'placement|requested slot)\b', question, re.I):
        return False
    lowered = answer.lower()
    if any(fragment in lowered for fragment in NO_EVIDENCE_PHRASES):
        return False
    if answer.endswith('?') and len(answer) < 240:
        # Source is asking for essential missing details, not providing facts.
        return False
    return True


def fairness_evidence(files: dict | None, *, include_metrics: bool) -> str:
    """Return only statistics attached to the currently selected valid version."""
    if not include_metrics or not files:
        return ''
    raw = files.get('quality_summary.json')
    if not raw:
        return ''
    try:
        document = json.loads(raw.decode('utf-8'))
        measures = document.get('metrics', {})
        keys = (
            'teacher_gap_periods', 'section_avoidable_gap_proxy',
            'teacher_daily_overload_periods', 'late_session_periods',
            'preferred_free_slot_periods', 'total_teacher_weekday_load_spread',
        )
        selected = {key: measures[key] for key in keys
                    if key in measures and isinstance(measures[key], (int, float))}
        return json.dumps(selected, sort_keys=True)[:800] if selected else ''
    except (ValueError, KeyError, TypeError, UnicodeError, AttributeError):
        return ''


def augment_verified_answer(question: str, answer: str, *, files=None, model=None,
                            explainer=None) -> tuple[str, bool]:
    """Return (rendered_response, Groq_actually_used).

    Preserve the original verified answer verbatim. If Groq is unavailable or
    evidence is insufficient, silently retain it. Never publish an AI-supplied
    timetable fact, edit or feasibility determination as independently verified.
    ``explainer`` may be injected for deterministic unit testing.
    """
    if not eligible_for_analysis(question, answer):
        return answer, False
    from timetable_cloud_ai import analyze_verified, DEFAULT_MODEL
    explain = explainer or analyze_verified
    metrics = fairness_evidence(files, include_metrics=bool(re.search(
        r'\b(fairness|fairer|gaps?|load|workload|compare|comparison|better|worse|optimi[sz])\b',
        question, re.I)))
    evidence = 'VERIFIED TIMETABLE TOOL ANSWER:\n' + answer[:1450]
    if metrics:
        evidence += '\nCURRENT SELECTED TIMETABLE METRICS:\n' + metrics
    text, error = explain(
        'Explain the verified answer to this coordinator question and give useful '
        'interpretation or trade-offs only where the evidence supports them. '
        'Do not invent missing counts, personal preferences, other timetables, '
        'free periods or valid class moves. If asked why a load is high, '
        'distinguish measured workload from an unknown institutional cause. '
        'Question: ' + question[:240],
        evidence,
        model=model or DEFAULT_MODEL,
    )
    if error or not isinstance(text, str) or not text.strip():
        return answer, False
    explanation = text.strip()
    return (answer + '\n\n---\n\n**Groq AI analysis (based on verified data)**\n\n'
            + explanation + '\n\n*Timetable records and numerical checks above remain authoritative.*'), True
