"""Publish only validated fairness improvements; retain verified baseline otherwise."""
from __future__ import annotations

import json

from timetable_fairness import fairness_score

VALID = {'PASS', 'PASS_WITH_LIMITATIONS'}


def select_published_result(baseline, candidate, caps, fairness_profile='balanced'):
    """Compare two independent runs, returning a safe published result.

    If a candidate fails to solve, allocate rooms, validate, or improve its
    measured fairness score, publish the *real baseline output files*, clearly
    labeled as a standard fallback. Never report a fabricated optimization.
    """
    if baseline.get('status_code') != 0 or baseline.get('independent_status') not in VALID:
        return candidate
    reason = None
    if candidate.get('status_code') != 0 or candidate.get('independent_status') not in VALID:
        reason = 'The fairness improvement search did not produce an independently validated timetable.'
    elif ('quality_summary.json' not in baseline.get('files', {})
          or 'quality_summary.json' not in candidate.get('files', {})):
        reason = 'The fairness comparison cannot be measured because a quality report is missing.'
    else:
        std = json.loads(baseline['files']['quality_summary.json'].decode('utf-8'))
        new = json.loads(candidate['files']['quality_summary.json'].decode('utf-8'))
        m = new['metrics']
        if caps and (m['preferred_free_slot_periods'] > caps['avoid']
                     or m['late_session_periods'] > caps['late']):
            reason = 'The candidate exceeds the validated baseline quality limits.'
        elif fairness_score(new, fairness_profile) >= fairness_score(std, fairness_profile):
            reason = 'No improvement in measured faculty/section fairness was found.'
    if reason is None:
        return candidate
    return {
        'status_code': baseline['status_code'],
        'independent_status': baseline['independent_status'],
        'files': dict(baseline['files']),
        'logs': ('FAIRNESS ATTEMPT (NOT PUBLISHED)\n' + candidate.get('logs', '')
                 + '\nVALIDATED STANDARD FALLBACK\n' + baseline.get('logs', '')),
        'variant': 'standard_fallback',
        'fell_back_to_standard': True,
        'fallback_reason': reason,
        'fairness_attempt_code': candidate.get('status_code'),
        'comparison_unavailable': reason + ' Standard timetable retained; no fairness improvement is claimed.',
    }
