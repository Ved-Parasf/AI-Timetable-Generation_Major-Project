"""Soft fairness objectives and reproducible timetable-quality measurements.

This module never changes course hours, room types, student memberships, faculty
allocations or solver hard constraints. Only *optional* auxiliary variables and
soft objective terms are added to an existing CP-SAT model.

Important distinction: a faculty member's TOTAL assigned weekly load cannot be
rebalanced without changing teacher-course assignments. This feature instead
balances how their fixed teaching load is distributed over the five weekdays.
"""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

DAYS = ('MON', 'TUE', 'WED', 'THU', 'FRI')
WEIGHTS = {
    'teacher_gap': 4,
    'section_gap': 3,
    'teacher_daily_overload': 5,
    'late_period': 1,
}
# Coordinator-selectable, bounded objective profiles. Cloud AI only chooses
# from these IDs; it cannot write constraints or arbitrary coefficients.
FAIRNESS_PROFILES = {
    'balanced': dict(WEIGHTS),
    'faculty_gaps': {'teacher_gap': 9, 'section_gap': 2,
                     'teacher_daily_overload': 5, 'late_period': 1},
    'section_gaps': {'teacher_gap': 2, 'section_gap': 9,
                     'teacher_daily_overload': 4, 'late_period': 1},
    'daily_balance': {'teacher_gap': 3, 'section_gap': 2,
                      'teacher_daily_overload': 12, 'late_period': 1},
}
FAIRNESS_PROFILE_LABELS = {
    'balanced': 'Balanced fairness (existing default)',
    'faculty_gaps': 'Reduce faculty timetable gaps',
    'section_gaps': 'Reduce section timetable gaps',
    'daily_balance': 'Balance faculty teaching days',
}


def available_focuses_from_policy_report(policy_report):
    """Do not advertise section-gap optimization if Excel disabled its soft rule.

    Standard reports mark an enabled student-gap preference CONDITIONAL:
    it becomes active only when the separate Fairness run is requested.
    """
    policies = policy_report.get('policies', []) if isinstance(policy_report, dict) else []
    section_enabled = any(
        isinstance(row, dict)
        and row.get('policy_name') == 'minimize_student_gaps'
        and row.get('status') in ('APPLIED', 'CONDITIONAL', 'DATA_CHECKED')
        for row in policies
    )
    return ('balanced', 'faculty_gaps', 'section_gaps', 'daily_balance') if section_enabled else (
        'balanced', 'faculty_gaps', 'daily_balance')


def weights_for_profile(profile='balanced'):
    """Validate and return a fresh profile; no unbounded AI coefficients."""
    if profile not in FAIRNESS_PROFILES:
        raise ValueError('Unsupported fairness focus: ' + str(profile))
    return dict(FAIRNESS_PROFILES[profile])


def priority_multipliers(sessions, groups, slots, policies=None, fairness_profile='balanced'):
    """Proven-safe objective scales for a POLICY > LATE > FAIRNESS ordering.

    Fairness terms returned by add_fairness_objective() are nonnegative in
    aggregate: each section lunch credit can cancel only an actual gap.
    Their total cannot exceed the bound below.  A single extra policy-avoided
    occupancy therefore cannot be traded for arbitrarily many gap improvements.

    This priority choice is explicit and OPTIONAL; 'balanced' mode keeps the
    workbook's original numeric soft coefficients instead.
    """
    # Gap variables are indexed by the period *number* span. Using calendar
    # span (rather than a fixed five-day or 8-period assumption) gives a safe
    # bound even for universities using nonconsecutive period numbers.
    w = weights_for_profile(fairness_profile)
    periods_by_day = defaultdict(list)
    for slot in slots:
        periods_by_day[_clean(slot.get('day'))].append(_period(slot['period']))
    period_span = sum(max(p) - min(p) + 1 for p in periods_by_day.values() if p)
    n_teachers = len({s.faculty for s in sessions})
    n_sections = len({_parent_of(s.group, groups) for s in sessions
                      if _parent_of(s.group, groups) is not None})
    total_periods = sum(s.duration for s in sessions)
    fairness_bound = (period_span * n_teachers * w['teacher_gap']
                      + period_span * n_sections * w['section_gap']
                      + total_periods * w['teacher_daily_overload'])
    late_multiplier = fairness_bound + 1
    avoid_multiplier = (total_periods * late_multiplier + fairness_bound + 1)
    # Keep every possible full objective below signed CP-SAT integer bounds.
    if total_periods * (avoid_multiplier + late_multiplier) > (2**62):
        raise ValueError('Timetable too large for the protected priority coefficients')
    return avoid_multiplier, late_multiplier, fairness_bound


def quality_caps_from_report(report):
    """Return *actual* measured baseline caps, not predicted estimates."""
    metrics = report.get('metrics', {})
    caps = {
        'avoid': int(metrics['preferred_free_slot_periods']),
        'late': int(metrics['late_session_periods']),
    }
    if any(value < 0 for value in caps.values()):
        raise ValueError('A comparison quality cap cannot be negative')
    return caps


def _clean(x):
    return str(x if x is not None else '').strip()


def _period(v):
    return int(float(v))


def _parent_of(group_id, groups):
    """Count only a parent section's own classes and explicitly named batches.

    We intentionally do NOT count all mixed-section elective sessions as though
    every student from every contributing original section attended them. The
    workbook lacks individual enrollment IDs, and doing so would distort gaps.
    """
    group = groups.get(group_id, {})
    if _clean(group.get('group_kind')) == 'PARENT':
        return group_id
    parent = _clean(group.get('parent_group_id'))
    if _clean(group.get('group_kind')) == 'LAB_BATCH' and parent in groups:
        return parent
    return None


def add_fairness_objective(model, sessions, groups, slots, choices, policies=None, fairness_profile='balanced', *, include_weekday_spread=False):
    """Return extra CP-SAT objective terms; original hard constraints remain.

    `choices[i]` contains (start_slot_index, occupied_slot_indices, BoolVar).
    A gap means an unoccupied period between earlier and later occupied periods.
    For student sections, one intra-day lunch gap in periods 4/5/6 is exempted.
    """
    w = weights_for_profile(fairness_profile)
    days = tuple(policies.days) if policies is not None else tuple(dict.fromkeys(_clean(s.get('day')) for s in slots))
    midday = set(policies.lunch_periods) if policies else {4,5,6}
    by_teacher = defaultdict(list)
    by_section = defaultdict(list)
    for sess, options in zip(sessions, choices):
        parent = _parent_of(sess.group, groups)
        for _, occupied, var in options:
            for ix in occupied:
                by_teacher[(sess.faculty, ix)].append(var)
                if parent is not None:
                    by_section[(parent, ix)].append(var)

    per_day = defaultdict(list)
    for ix, slot in enumerate(slots):
        per_day[_clean(slot.get('day'))].append((int(float(slot['period'])), ix))
    for day in per_day:
        per_day[day].sort()

    def presence_for(terms, prefix):
        by_entity = defaultdict(dict)
        entities = sorted({who for who, _ in terms})
        for entity_index, entity in enumerate(entities):
            for day in days:
                for period, ix in per_day[day]:
                    lits = terms.get((entity, ix), [])
                    if lits:
                        b = model.new_bool_var(f'{prefix}_occ_{entity_index}_{days.index(day)}_{period}')
                        model.add_max_equality(b, lits)
                        by_entity[(entity, day)][period] = b
        return by_entity

    teacher_occ = presence_for(by_teacher, 'ft')
    section_occ = presence_for(by_section, 'fs')
    terms = []

    def gap_terms(occupancy, label, weight, exempt_midday=False):
        for index, ((entity, day), periods) in enumerate(sorted(occupancy.items())):
            if len(periods) < 2:
                continue
            available = sorted(periods)
            day_gap_vars = []
            midday_gap_vars = []
            for p in range(min(available) + 1, max(available)):
                before = [periods[k] for k in available if k < p]
                after = [periods[k] for k in available if k > p]
                if not before or not after:
                    continue
                left = model.new_bool_var(f'{label}_left_{index}_{p}')
                right = model.new_bool_var(f'{label}_right_{index}_{p}')
                gap = model.new_bool_var(f'{label}_gap_{index}_{p}')
                model.add_max_equality(left, before)
                model.add_max_equality(right, after)
                now = periods.get(p, 0)
                # gap == before AND after AND NOT occupied_now
                model.add(gap <= left)
                model.add(gap <= right)
                model.add(gap + now <= 1)
                model.add(gap >= left + right - now - 1)
                day_gap_vars.append(gap)
                if p in midday:
                    midday_gap_vars.append(gap)
            if day_gap_vars:
                terms.append(weight * sum(day_gap_vars))
                if exempt_midday and midday_gap_vars:
                    lunch_credit = model.new_bool_var(f'{label}_lunch_exemption_{index}')
                    model.add_max_equality(lunch_credit, midday_gap_vars)
                    terms.append(-weight * lunch_credit)

    gap_terms(teacher_occ, 'teacher', w['teacher_gap'])
    if policies is None or policies.fairness_gap_enabled:
        gap_terms(section_occ, 'section', w['section_gap'], exempt_midday=True)

    # Model daily overload in excess of ceil(assigned_weekly_periods / 5).
    # Unlike overall weekly loads, distribution is a real scheduling decision.
    weekly_periods = defaultdict(int)
    for sess in sessions:
        weekly_periods[sess.faculty] += sess.duration
    for index, teacher in enumerate(sorted(weekly_periods)):
        threshold = math.ceil(weekly_periods[teacher] / max(1, len(days)))
        for day in days:
            occ = teacher_occ.get((teacher, day), {})
            if not occ:
                continue
            overload = model.new_int_var(0, len(per_day[day]), f'faculty_over_{index}_{day}')
            model.add(overload >= sum(occ.values()) - threshold)
            terms.append(w['teacher_daily_overload'] * overload)

    # The measured balanced fairness score also includes each faculty's
    # maximum-minus-minimum weekday load. Optimize this exact measured component
    # only in guarded UCB1 mode, preserving the original/manual Fairness model.
    if include_weekday_spread:
        max_day_periods = max((len(v) for v in per_day.values()), default=0)
        for faculty_index, teacher in enumerate(sorted(weekly_periods)):
            loads = []
            for day_index, day in enumerate(days):
                occ = teacher_occ.get((teacher, day), {})
                load = model.new_int_var(0, max_day_periods,
                                         f'guarded_teacher_load_{faculty_index}_{day_index}')
                model.add(load == sum(occ.values()))
                loads.append(load)
            max_load = model.new_int_var(0, max_day_periods,
                                         f'guarded_teacher_max_{faculty_index}')
            min_load = model.new_int_var(0, max_day_periods,
                                         f'guarded_teacher_min_{faculty_index}')
            model.add_max_equality(max_load, loads)
            model.add_min_equality(min_load, loads)
            terms.append(max_load - min_load)

    # An additional low-weight preference to avoid late teaching periods.
    # Thursday's existing penalty is still added by the main solver unchanged.
    # Late-slot policy already contributes in the base solver, including
    # standard runs. Do not count it twice when fairness is enabled.
    if policies is None:
      for sess, options in zip(sessions, choices):
        for _, occupied, var in options:
            late_count = sum(1 for ix in occupied if _period(slots[ix]['period']) in (7, 8))
            if late_count:
                terms.append(w['late_period'] * late_count * var)
    return terms


def _internal_gap_count(periods, lunch_periods=(4,5,6)):
    if len(periods) < 2:
        return 0, 0
    gaps = set(range(min(periods), max(periods) + 1)) - set(periods)
    lunch_credit = 1 if gaps & set(lunch_periods) else 0
    return len(gaps), len(gaps) - lunch_credit


def quality_report(rows, groups, mode='standard', policies=None, fairness_profile='balanced'):
    """Summarize *actual CSV output*, never estimate quality from CP-SAT status.

    Returned totals use distinct owner/weekday/period occupancy, so two rows
    from a two-hour practical never count as two duplicate sessions.
    """
    days = tuple(policies.days) if policies else DAYS
    lunch = tuple(policies.lunch_periods) if policies else (4,5,6)
    avoid = set(policies.avoid_slot_ids) if policies else set()
    late_ids = set(policies.late_slot_ids) if policies else set()
    teachers = defaultdict(lambda: defaultdict(set))
    sections = defaultdict(lambda: defaultdict(set))
    for row in rows:
        day = _clean(row.get('day')).upper()
        if day not in days:
            continue
        try:
            period = _period(row.get('period'))
        except (TypeError, ValueError):
            continue
        fid = _clean(row.get('faculty_id'))
        if fid:
            teachers[fid][day].add(period)
        section = _parent_of(_clean(row.get('group_id')), groups)
        if section:
            sections[section][day].add(period)

    teacher_rows = []
    total_teacher_gaps = 0
    total_overload = 0
    total_spread = 0
    for teacher in sorted(teachers):
        daily = [len(teachers[teacher].get(day, set())) for day in days]
        total = sum(daily)
        threshold = math.ceil(total / max(1,len(days)))
        overload = sum(max(0, n - threshold) for n in daily)
        gaps = sum(_internal_gap_count(teachers[teacher].get(day, set()),lunch)[0] for day in days)
        spread = max(daily) - min(daily)
        total_teacher_gaps += gaps
        total_overload += overload
        total_spread += spread
        teacher_rows.append(dict({'faculty_id': teacher, 'weekly_periods': total},
            **{day.lower(): len(teachers[teacher].get(day,set())) for day in days},
            gap_periods=gaps, overload_above_daily_target=overload, weekday_load_spread=spread))

    section_rows = []
    total_section_gaps = 0
    for section in sorted(sections):
        daily = [len(sections[section].get(day, set())) for day in days]
        gaps = sum(_internal_gap_count(sections[section].get(day, set()),lunch)[1] for day in days)
        total_section_gaps += gaps
        section_rows.append(dict({'section_id':section,'weekly_occupied_periods':sum(daily)},
            **{day.lower():len(sections[section].get(day,set())) for day in days},
            avoidable_gap_proxy=gaps))

    # Count timeslot usage as *distinct scheduled sessions*. Overlapping lab
    # batches are legitimate different sessions, not repeated CSV rows.
    late_periods = 0
    thursday_afternoon = 0
    seen = set()
    for row in rows:
        key = (_clean(row.get('session_id')), _clean(row.get('day')), _clean(row.get('period')))
        if key in seen:
            continue
        seen.add(key)
        try:
            p = _period(row.get('period'))
        except (TypeError, ValueError):
            continue
        slot_id = next((sid for sid,sl in (policies.slot_index.items() if policies else [])
                        if _clean(sl.get('day'))==_clean(row.get('day')) and _period(sl.get('period'))==p), '')
        if slot_id in late_ids or (policies is None and p in (7,8)):
            late_periods += 1
        if slot_id in avoid or (policies is None and _clean(row.get('day'))=='THU' and p in (7,8)):
            thursday_afternoon += 1

    return {
        'mode': mode,
        'metrics': {
            'teacher_gap_periods': total_teacher_gaps,
            'section_avoidable_gap_proxy': total_section_gaps,
            'teacher_daily_overload_periods': total_overload,
            'total_teacher_weekday_load_spread': total_spread,
            'late_session_periods': late_periods,
            'thursday_afternoon_session_periods': thursday_afternoon, # legacy key now counts policy-avoided slots
            'preferred_free_slot_periods': thursday_afternoon,
        },
        'faculty': teacher_rows,
        'sections': section_rows,
        'limitations': [
            'Counts measure the schedule output, not a guarantee of improved quality.',
            'Faculty total weekly teaching periods are fixed by the Excel assignments; only daily distribution is optimized.',
            'Section gaps use the section and its named lab batches. Mixed elective attendance is excluded without student-level IDs.',
            f'The section gap proxy excludes one lunch-window break ({lunch}) between classes per day.',
            'Synthetic rooms, provisional faculty and unverified student memberships remain prototype assumptions.',
            'A FEASIBLE solution is not necessarily optimal; compare runs with equivalent solver time limits.',
        ],
        'weights': weights_for_profile(fairness_profile),
        'fairness_focus': fairness_profile,
    }


def quality_text(report):
    lines = [f"TIMETABLE QUALITY REPORT — {report.get('mode', 'unknown').upper()}", '=' * 58]
    for name, value in report['metrics'].items():
        lines.append(f'{name}: {value}')
    lines += ['', 'LIMITATIONS'] + ['- ' + s for s in report.get('limitations', [])]
    return '\n'.join(lines) + '\n'


def save_quality_files(output_dir, report, write_csv):
    output_dir = Path(output_dir)
    (output_dir / 'quality_summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    (output_dir / 'quality_summary.txt').write_text(quality_text(report), encoding='utf-8')
    for name, key in [('quality_faculty.csv', 'faculty'), ('quality_sections.csv', 'sections')]:
        table = report[key]
        if table:
            write_csv(output_dir / name, list(table[0]), table)


def quality_comparison(standard, fairness):
    if not standard or not fairness:
        raise ValueError('Both actual timetable quality reports are required.')
    labels = {
        'teacher_gap_periods': 'Faculty gap periods',
        'section_avoidable_gap_proxy': 'Section gaps (excl. one lunch gap)',
        'teacher_daily_overload_periods': 'Daily overload periods',
        'total_teacher_weekday_load_spread': 'Faculty weekday load spread (sum)',
        'late_session_periods': 'Late session periods',
        'thursday_afternoon_session_periods': 'Policy-avoided occupied periods',
    }
    output = []
    for key, label in labels.items():
        s = standard['metrics'][key]
        f = fairness['metrics'][key]
        output.append({'metric': label, 'standard': s, 'fairness': f,
                       'change': f - s, 'improvement': s - f})
    return output


def fairness_score(report, fairness_profile='balanced'):
    """Comparable aggregate of measured gap/balance metrics, lower is better.

    This is used only to decide whether a newly validated candidate represents
    a *measurable improvement* over the independently validated standard
    timetable. It never overrides institutional hard or soft-avoid constraints.
    """
    m = report['metrics']
    w = weights_for_profile(fairness_profile)
    return (w['teacher_gap'] * int(m['teacher_gap_periods'])
            + w['section_gap'] * int(m['section_avoidable_gap_proxy'])
            + w['teacher_daily_overload'] * int(m['teacher_daily_overload_periods'])
            + int(m['total_teacher_weekday_load_spread']))
