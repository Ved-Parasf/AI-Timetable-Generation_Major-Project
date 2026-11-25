"""Source/version and verified Standard-vs-Fairness comparisons for the chat.

Never compare a saved historical CSV with metrics from a different generation.
All numbers are sourced directly from the two on-disk quality reports.
"""
import json
import re


METRICS = [
    ('Faculty gaps', 'teacher_gap_periods'),
    ('Section gaps', 'section_avoidable_gap_proxy'),
    ('Late session periods', 'late_session_periods'),
    ('Thursday soft-avoid periods', 'preferred_free_slot_periods'),
]


def answer_source_query(question, *, source_label='', comparison_files=None):
    q=re.sub(r'\s+', ' ', (question or '').lower()).strip()
    if (re.search(r'\b(which|what)\s+(?:timetable|schedule|version)\b.*\b(using|analysing|analyzing|reading|selected|looking at)\b', q)
            or re.search(r'\b(current|selected|active) (?:timetable|schedule|version)\b', q)
            or re.search(r'\b(what|which) (?:version|schedule) (?:am i|are you) (?:on|looking at|using)\b',q)):
        return (f'**I am analyzing {source_label or "the currently selected timetable"}.** '
                'Every factual answer uses that version’s timetable CSV.')
    if not (re.search(r'\b(compare|comparison|which is better|difference between|fewer gaps)\b',q)
            and re.search(r'\b(standard|fairness|optimization|optimized|both versions|two versions)\b',q)):
        return None
    if not comparison_files:
        return ('I can compare Standard and Fairness only when both were generated '
                'and independently validated in the current run. Comparing saved versions '
                'requires explicitly selecting both historical timetables.')
    baseline=comparison_files.get('baseline_quality_summary.json')
    fairness=comparison_files.get('quality_summary.json')
    if baseline and fairness:
        try:
            std=json.loads(baseline.decode('utf-8'))['metrics']
            opt=json.loads(fairness.decode('utf-8'))['metrics']
            lines=[]
            for label, key in METRICS:
                if key in std and key in opt:
                    a,b=int(std[key]),int(opt[key])
                    lines.append(f'- **{label}:** Standard {a}, Fairness {b} ({b-a:+d})')
            if lines:
                return ('**Standard vs Fairness — measured timetable quality**\n\n' +
                        '\n'.join(lines) + '\n\nNegative changes mean fewer occurrences. '
                        'These objective metrics do not establish a universally better timetable.')
        except (KeyError,TypeError,ValueError,UnicodeError):
            pass
    return ('A two-version quality comparison is unavailable for this run. '
            'Generate and validate both Standard and Fairness, then inspect Results → Quality & fairness.')
