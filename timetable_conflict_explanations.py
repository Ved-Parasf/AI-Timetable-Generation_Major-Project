"""Evidence-locked, human-readable explanations of proposed class moves.

The LLM may choose which verified reason to emphasize first; *never* display
model-generated conflict prose. All factual claims come from the rescheduler's
requested-slot diagnostics and institution policy configuration. In particular,
obstacles from OTHER candidate slots must not be attributed to the requested
slot. No timetable mutation occurs here.
"""
from __future__ import annotations

import re

_LUNCH = re.compile(
    r'^(?P<section>.+?):\s*insufficient lunch periods on\s+(?P<day>[A-Za-z]{3})\s*'
    r'\(policy window\s*\((?P<periods>[\d,\s]+)\)\)\s*$', re.IGNORECASE)
_DAYS = {'MON': 'Monday', 'TUE': 'Tuesday', 'WED': 'Wednesday',
         'THU': 'Thursday', 'FRI': 'Friday', 'SAT': 'Saturday', 'SUN': 'Sunday'}


def _reason_text(reason, *, lunch_periods=(), lunch_free_count=0):
    """Explain a known validator reason without inventing additional causes."""
    original = str(reason or '').strip()
    m = _LUNCH.fullmatch(original)
    if m:
        listed = tuple(int(n) for n in re.findall(r'\d+', m.group('periods')))
        # The diagnostic is authoritative about the requested period window;
        # policy config is authoritative about the required count. Never hardcode
        # 'every period 4–6 is closed' (that is false for flexible lunch).
        if lunch_periods and set(listed) != set(int(x) for x in lunch_periods):
            return '**Lunch requirement.** ' + original
        required = int(lunch_free_count or 0)
        periods = ', '.join(f'P{p}' for p in listed)
        day = _DAYS.get(m.group('day').upper(), m.group('day'))
        base = (f'**Lunch requirement — {m.group("section")} on {day}.** '
                f'The requested class move would leave too few free lunch periods '
                f'within {periods}. ')
        if required > 0:
            base += (f'This policy requires at least {required} free '
                     f'period{"s" if required != 1 else ""} within that window; '
                     'classes are not forbidden throughout the entire window.')
        else:
            base += ('This is a section-level lunch requirement, not a blanket '
                     'ban on teaching during the listed periods.')
        return base
    low = original.lower()
    if 'teacher' in low or 'faculty' in low:
        label = 'Teacher availability or clash'
    elif any(word in low for word in ('student', 'group', 'batch', 'section overlap')):
        label = 'Student/group scheduling conflict'
    elif any(word in low for word in ('room', 'laborator', 'classroom', 'capacity')):
        label = 'Room or capacity restriction'
    elif any(word in low for word in ('elective', 'reserved')):
        label = 'Institutional reservation'
    elif any(word in low for word in ('practical', 'consecutive', 'continuous')):
        label = 'Session duration restriction'
    else:
        label = 'Verified scheduling restriction'
    # Report only the actual validated reason. No added person/course/slot.
    return f'**{label}.** {original}'


def render_checked_conflicts(findings: dict, *, lunch_periods=(),
                             lunch_free_count=0, focus_index=None) -> str:
    """Return complete explanation for the REQUESTED slot, no invented claims.

    `focus_index` is optional model output and can ONLY alter presentation order.
    The reason list itself is wholly sourced from the validator/rescheduler.
    """
    reasons = [str(x).strip() for x in findings.get('requested_reasons', [])
               if isinstance(x, str) and str(x).strip()]
    requested = str(findings.get('requested_slot') or '').strip()
    count = findings.get('validated_alternative_count')
    lines = []
    if findings.get('requested_slot_allowed'):
        lines.append('**Requested placement:** independently validated as an alternative.'
                     ' A change still requires coordinator approval.')
    else:
        lines.append('**Requested placement:** not a validated alternative.'
                     + (f' ({requested})' if requested else ''))
    if reasons:
        if type(focus_index) is int and 0 <= focus_index < len(reasons):
            indices = [focus_index] + [i for i in range(len(reasons)) if i != focus_index]
        else:
            indices = list(range(len(reasons)))
        lines.extend('- ' + _reason_text(reasons[i], lunch_periods=lunch_periods,
                                         lunch_free_count=lunch_free_count)
                     for i in indices)
    elif not findings.get('requested_slot_allowed'):
        lines.append('The available diagnostics do not establish a more specific '
                     'reason for this requested placement.')
    if type(count) is int and count >= 0:
        lines.append(f'**{count} independently validated alternative starting slot(s)** '
                     'were reported by the rescheduler for review.')
    lines.append('**Next step:** Review a validated alternative, then confirm it '
                 'through the coordinator approval workflow. **No timetable change has been made.**')
    return '\n\n'.join(lines)
