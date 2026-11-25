"""Typed, fail-closed interpretation of Institution_Policies.

This file is the single adapter between institution-specific Excel policy rows
and the CP-SAT code.  Policies are NOT instructions to eval/execute; only the
explicitly recognized, validated value syntax below is supported.

A row marked 'applied' is connected to a model/check; 'verified_from_data'
means it is checked against explicit Courses/Groups/Assignments; 'conditional'
means its soft objective can be disabled by the user's run setting.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


class PolicyError(ValueError):
    pass


def txt(value):
    return str(value if value is not None else '').strip()


def flag(value):
    if txt(value).lower() in ('yes','true','1','y'):
        return True
    if txt(value).lower() in ('no','false','0','n'):
        return False
    raise PolicyError(f'Expected Yes/No or true/false, received {value!r}')


def number(value, name, minimum=0):
    try:
        x = int(str(value))
    except (TypeError, ValueError):
        raise PolicyError(f'{name} must be a whole number: {value!r}')
    if x < minimum:
        raise PolicyError(f'{name} must be >= {minimum}: {x}')
    return x


def hm(value):
    try:
        return int(datetime.strptime(txt(value), '%H:%M').strftime('%H')) * 60 + int(txt(value)[3:5])
    except ValueError:
        raise PolicyError(f'Expected 24-hour HH:MM time, got {value!r}')


def day_key(day):
    return txt(day).upper()


SUPPORTED = {
    'avoid_thursday_afternoon_academic_classes',
    'reserve_open_elective_days', 'reserve_open_elective_start',
    'reserve_open_elective_end', 'open_elective_min_semester',
    'fixed_lunch_slot_per_section', 'flexible_lunch_one_free_midday_period',
    'mentoring_frequency_weeks', 'lab_contiguous_periods',
    'lab_same_room_entire_session', 'lab_batches_per_section',
    'prevent_teacher_overlap', 'prevent_room_overlap',
    'prevent_student_group_overlap', 'require_room_capacity',
    'faculty_max_daily_sessions', 'minimize_student_gaps',
    'balance_undesirable_slots', 'language_enrollee_no_other_class_overlap',
    'mandarin_two_groups_shared_theory', 'german_two_groups_share_theory',
    'language_ltp_controls_session_counts', 'faculty_max_weekly_contact_hours',
    'faculty_max_consecutive_slots', 'room_facilities_prototype_mode',
    'unverified_practical_scope_prototype_switch',
    'unverified_language_teacher_and_pairing_prototype',
    'course_room_type_by_session', 'classroom_practical_exclusive_block',
    'thursday_afternoon_class_penalty_per_period',
    # Portable equivalents, useful for future institution datasets.
    'avoid_timeslots', 'undesirable_slot_penalty_per_period',
    'elective_course_codes', 'shared_theory_course_structure',
    'open_elective_window_mode',
    'fixed_non_teaching_blocks', 'practical_duration_exact_minutes',
}


def effective_group_semester(group_id, groups):
    """Most restrictive semester among a session group and its source sections.

    A mixed cohort can belong to several original sections; its own semester
    field alone is not sufficient to decide whether a reserved window applies.
    """
    def sem(record):
        try:
            return int(float(record.get('semester') or 0))
        except (ValueError, TypeError):
            return 0

    record = groups.get(group_id, {})
    semesters = [sem(record)]
    references = [txt(record.get('parent_group_id'))]
    references += [part.split('=')[0].strip()
                   for part in txt(record.get('synthetic_membership_by_original_section')).split(';')]
    for ref in references:
        member = groups.get(ref)
        if member is not None:
            semesters.append(sem(member))
            parent = groups.get(txt(member.get('parent_group_id')))
            if parent is not None:
                semesters.append(sem(parent))
    return max(semesters)


def check_elective_reservation(data, rows, policies):
    """Independently examine finished CSV rows; return actionable violations."""
    groups = {txt(x.get('group_id')): x for x in data.get('Student_Groups', [])}
    slot_ids = {(day_key(s.get('day')), int(float(s.get('period')))): txt(s.get('slot_id'))
                for s in data.get('Time_Slots', [])}
    violations = []
    for r in rows:
        gid = txt(r.get('group_id'))
        day = day_key(r.get('day'))
        try:
            period = int(float(r.get('period')))
        except (TypeError, ValueError):
            period = 0
        if gid not in groups:
            violations.append({'error': 'UNKNOWN_GROUP', **r})
            continue
        slot_id = slot_ids.get((day, period))
        if not slot_id:
            violations.append({'error': 'UNKNOWN_SLOT', **r})
            continue
        semester = effective_group_semester(gid, groups)
        is_allowed_elective = txt(r.get('course_id')) in policies.window_allowed_course_codes
        if policies.reserved({'slot_id': slot_id}, semester, is_allowed_elective):
            violations.append({'error': 'ELECTIVE_RESERVATION', **r,
                               'semester': semester})
    return violations


@dataclass
class PolicyConfig:
    days: tuple[str, ...]
    periods_by_day: dict[str, tuple[int, ...]]
    slot_index: dict[str, dict]
    active: dict[str, dict]
    audit: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    lunch_periods: tuple[int, ...] = ()
    lunch_free_count: int = 0
    mentor_weeks: int = 0
    mentor_courses: frozenset[str] = frozenset()
    practical_length: int = 2
    practical_exact_minutes: int | None = None
    fixed_non_teaching_blocks: tuple[tuple[str, int, int], ...] = ()
    elective_days: frozenset[str] = frozenset()
    elective_start: int = 0
    elective_end: int = 0
    elective_min_sem: int = 999
    elective_courses: frozenset[str] = frozenset()
    window_allowed_course_codes: frozenset[str] = frozenset()
    elective_window_mode: str = 'DISABLED'
    reserved_slot_ids: frozenset[str] = frozenset()
    avoid_slot_ids: frozenset[str] = frozenset()
    hard_closed_slot_ids: frozenset[str] = frozenset()
    avoid_weight: int = 0
    late_weight: int = 0
    late_slot_ids: frozenset[str] = frozenset()
    faculty_daily_cap: int | None = None
    faculty_weekly_hours_cap: int | None = None
    faculty_consecutive_cap: int | None = None
    language_conflicts: bool = False
    spread_theory: bool = True
    fairness_gap_enabled: bool = False
    enforcement: dict[str, bool] = field(default_factory=dict)

    def reserved(self, slot, semester, is_elective=False):
        """Interpret a workbook-configured Open Elective reservation.

        NO_CLASSES_ALLOWED: any session is forbidden.
        REGULAR_CLASSES_BLOCKED: only courses explicitly listed in the enabled
            ``elective_course_codes`` policy may occupy the reserved period.
        DISABLED: no reservation is enforced.

        ``is_elective`` means the caller verified the course code against
        ``window_allowed_course_codes``; course-type names alone do not grant
        an exemption.  The model, validator and report use the SAME policy.
        """
        if self.elective_window_mode == 'DISABLED':
            return False
        if int(semester) < self.elective_min_sem or txt(slot.get('slot_id')) not in self.reserved_slot_ids:
            return False
        return not (self.elective_window_mode == 'REGULAR_CLASSES_BLOCKED' and is_elective)

    def note(self, name, status, detail):
        r = self.active.get(name)
        if r:
            self.audit.append({'policy_id': txt(r.get('policy_id')),
                               'policy_name': name, 'constraint_type': txt(r.get('constraint_type')),
                               'policy_value': txt(r.get('policy_value')),
                               'status': status, 'implementation': detail})

    def export(self, output):
        output = Path(output)
        self.audit.sort(key=lambda a: (int(''.join(ch for ch in a['policy_id'] if ch.isdigit()) or '99999'), a['policy_name']))
        output.mkdir(parents=True, exist_ok=True)
        (output / 'policy_compliance.json').write_text(json.dumps({
            'summary': {'enabled_rows': len(self.active),
                        'connected_or_checked': sum(a['status'] in ('APPLIED','DATA_CHECKED','CONDITIONAL') for a in self.audit),
                        'unsupported_or_invalid': len(self.errors)},
            'policies': self.audit, 'errors': self.errors, 'warnings': self.warnings,
            'calendar_days': list(self.days), 'lunch_periods': list(self.lunch_periods),
            'reserved_elective_slot_ids': sorted(self.reserved_slot_ids),
            'avoided_slot_ids': sorted(self.avoid_slot_ids),
            'hard_closed_slot_ids': sorted(self.hard_closed_slot_ids),
            'elective_courses': sorted(self.elective_courses),
            'open_elective_window_mode': self.elective_window_mode,
            'practical_duration_exact_minutes': self.practical_exact_minutes,
            'fixed_non_teaching_blocks': [
                {'label': label, 'start_time': f'{start//60:02}:{start%60:02}',
                 'end_time': f'{end//60:02}:{end%60:02}'}
                for label, start, end in self.fixed_non_teaching_blocks],
            'allowed_window_course_codes': sorted(self.window_allowed_course_codes),
        }, indent=2), encoding='utf-8')
        lines = ['INSTITUTION POLICY INTERPRETATION REPORT', '='*60,
                 f'Enabled policies: {len(self.active)}',
                 f'Issues: {len(self.errors)}',
                 'Calendar days: ' + ', '.join(self.days),
                 'Fixed non-teaching blocks: ' + (
                     '; '.join(f'{name} {start//60:02}:{start%60:02}-{end//60:02}:{end%60:02}'
                               for name,start,end in self.fixed_non_teaching_blocks)
                     or '(none)'),
                 'Exact practical duration (minutes): ' + str(self.practical_exact_minutes or 'Not enabled'),
                 'Lunch periods: ' + (', '.join(map(str,self.lunch_periods)) if self.lunch_periods else 'Not enabled'),
                 'Open Elective mode: ' + self.elective_window_mode,
                 'Allowed course codes during reserved window: ' + (', '.join(sorted(self.window_allowed_course_codes)) or '(none)'),
                 'Reserved elective slots: ' + (', '.join(sorted(self.reserved_slot_ids)) or '(none)'),
                 'Avoided soft slots: ' + (', '.join(sorted(self.avoid_slot_ids)) or '(none)'),
                 'Hard-closed slots: ' + (', '.join(sorted(self.hard_closed_slot_ids)) or '(none)'),
                 '', 'POLICY AUDIT']
        lines += [f'{a["policy_id"]} | {a["policy_name"]} | {a["status"]}\n  {a["implementation"]}' for a in self.audit]
        lines += ['', 'ERRORS'] + (self.errors or ['None'])
        lines += ['', 'WARNINGS'] + (self.warnings or ['None'])
        (output/'policy_compliance.txt').write_text('\n'.join(lines), encoding='utf-8')


def _parse_course_spec(value):
    """COURSE_A,COURSE_B = 1-0-2; optional repeated clauses separated by ';'."""
    rules = {}
    for clause in txt(value).split(';'):
        if not clause.strip():
            continue
        match = re.fullmatch(r'\s*([A-Za-z0-9_,\s-]+)\s*=\s*(\d+)\s*-\s*(\d+)\s*-\s*(\d+)\s*', clause)
        if not match:
            raise PolicyError(f'Invalid language L-T-P specification: {clause!r}')
        for cid in match.group(1).split(','):
            if cid.strip():
                rules[cid.strip()] = '-'.join(match.group(i) for i in (2,3,4))
    return rules


def _parse_shared(value):
    # Accept both P20/P21 descriptions and new generic declaration.
    match = re.search(r'^\s*([A-Za-z0-9]+)\s*:\s*(\d+)\s*(?:lab|practical)\s*groups?\s*/\s*(?:ONE|(\d+))\s*shared\s*theor(?:y|ies)', txt(value), re.I)
    if not match:
        raise PolicyError(f'Expected COURSE: N lab groups / M shared theory, got {value!r}')
    return match.group(1), int(match.group(2)), int(match.group(3) or 1)


def load_policies(data, *, optimize_fairness=False):
    records = data.get('Institution_Policies', [])
    slots = data.get('Time_Slots', [])
    courses = {txt(x.get('course_id')): x for x in data.get('Courses', [])}
    groups = {txt(x.get('group_id')): x for x in data.get('Student_Groups', [])}
    assignments = data.get('Course_Assignments', [])
    days = tuple(dict.fromkeys(day_key(x.get('day')) for x in slots))
    periods = {day: tuple(sorted({number(x.get('period'),'period',1) for x in slots if day_key(x.get('day'))==day})) for day in days}
    slot_index = {txt(s.get('slot_id')): s for s in slots}
    active, errors, warnings = {}, [], []
    for row in records:
        name = txt(row.get('policy_name'))
        if not name:
            continue
        try:
            enabled = flag(row.get('enabled'))
        except PolicyError as ex:
            errors.append(f'{row.get("policy_id",name)}: {ex}');continue
        if not enabled:
            continue
        if name in active:
            errors.append(f'Duplicate enabled policy name: {name}');continue
        if name not in SUPPORTED:
            errors.append(f'UNSUPPORTED ENABLED POLICY {txt(row.get("policy_id"))}: {name}. Explicit implementation required before scheduling.')
            continue
        active[name] = row
    soft_names = {'thursday_afternoon_class_penalty_per_period',
                  'undesirable_slot_penalty_per_period', 'minimize_student_gaps',
                  'balance_undesirable_slots'}
    flexible_names = {'avoid_timeslots', 'avoid_thursday_afternoon_academic_classes'}
    for name,row in active.items():
        declared = txt(row.get('constraint_type')).upper()
        if declared not in ('HARD','SOFT'):
            errors.append(f'{row.get("policy_id")}: constraint_type must be Hard or Soft, received {row.get("constraint_type")!r}')
        elif name in soft_names and declared != 'SOFT':
            errors.append(f'{row.get("policy_id")}: {name} is a soft objective. Hard is unsupported; change type or implement a hard variant.')
        elif name not in soft_names | flexible_names and declared != 'HARD':
            errors.append(f'{row.get("policy_id")}: {name} is a hard rule. Soft interpretation is unsupported; change type or implement it explicitly.')
    cfg = PolicyConfig(days=days, periods_by_day=periods, slot_index=slot_index,
                       active=active, errors=errors, warnings=warnings)
    if not days or not slots:
        cfg.errors.append('Time_Slots must define at least one teaching period.')
    if len(slot_index) != len(slots):
        cfg.errors.append('Duplicate Time_Slots.slot_id values.')

    def get(name, default=None):
        return txt(active[name].get('policy_value')) if name in active else default

    def handle(name, status, fn):
        if name not in active:
            return None
        try:
            result, description = fn(get(name))
            cfg.note(name, status, description)
            return result
        except PolicyError as ex:
            cfg.errors.append(f'{active[name].get("policy_id")} ({name}): {ex}')
            cfg.note(name, 'INVALID', str(ex))
            return None

    def bool_rule(name, note, must_true=False):
        def check(raw):
            enabled = flag(raw)
            if must_true and not enabled:
                raise PolicyError('False is not supported: the solver guarantees this safety invariant. Remove/disable this policy only if changing the model deliberately.')
            return enabled, note
        return handle(name, 'APPLIED', check)

    avoid_names = ('avoid_timeslots','avoid_thursday_afternoon_academic_classes')
    avoid, blocked = set(),set()
    for name in avoid_names:
        def parse_avoid(raw, policy_name=name):
            found = {piece.strip().upper() for piece in raw.split(',') if piece.strip()}
            unknown = found - slot_index.keys()
            if unknown:
                raise PolicyError(f'Unknown slot IDs: {sorted(unknown)}')
            if txt(active[policy_name].get('constraint_type')).upper() == 'HARD':
                blocked.update(found)
                detail = f'Hard closure: do not schedule in {sorted(found)}.'
            else:
                avoid.update(found)
                detail = f'Soft-avoid explicit Time_Slots IDs {sorted(found)}; classes remain allowed.'
            return found,detail
        handle(name, 'APPLIED', parse_avoid)
    weight = handle('thursday_afternoon_class_penalty_per_period','APPLIED',
                    lambda raw: (number(raw,'avoid-slot penalty'), 'Soft penalty applied to every occupied avoided slot.'))
    portable_weight = handle('undesirable_slot_penalty_per_period','APPLIED',
                             lambda raw: (number(raw,'undesirable-slot penalty'), 'Penalty for each configured avoided slot.'))
    if (weight is not None or portable_weight is not None) and not avoid:
        cfg.errors.append('Soft-avoid penalty configured but no enabled avoid_timeslots or avoid_thursday_afternoon_academic_classes policy defines the slots.')
    if avoid & blocked:
        cfg.errors.append('Contradictory soft-avoid and hard-closure rules on the same slots: '+str(sorted(avoid & blocked)))
    cfg.hard_closed_slot_ids = frozenset(blocked)
    cfg.avoid_slot_ids = frozenset(avoid)
    cfg.avoid_weight = portable_weight if portable_weight is not None else (weight if weight is not None else (1 if avoid else 0))

    elective_days = handle('reserve_open_elective_days','APPLIED',
                           lambda raw: (frozenset(day_key(s) for s in raw.split(',') if s.strip()),
                                        'Reserve slots on the listed calendar days.'))
    start = handle('reserve_open_elective_start','APPLIED',lambda raw:(hm(raw),'Reservation begins at workbook clock time.'))
    end = handle('reserve_open_elective_end','APPLIED',lambda raw:(hm(raw),'Reservation ends at workbook clock time.'))
    min_sem = handle('open_elective_min_semester','APPLIED',
                     lambda raw:(number(raw,'elective minimum semester',1),'Applies reservation only to this semester and above.'))
    electoral = ('reserve_open_elective_days','reserve_open_elective_start','reserve_open_elective_end','open_elective_min_semester')
    if any(name in active for name in electoral) and not all(name in active for name in electoral):
        cfg.errors.append('Elective reservation requires all four policies: days, start, end, min semester.')
    if elective_days is not None and start is not None and end is not None and min_sem is not None:
        cfg.elective_days, cfg.elective_start, cfg.elective_end, cfg.elective_min_sem = elective_days,start,end,min_sem
        if start >= end:
            cfg.errors.append('Elective window start must be earlier than end.')
        if not elective_days <= set(days):
            cfg.errors.append('Elective days include days not present in Time_Slots.')
        # Use the actual spreadsheet clock times; do not rely on stale Yes/No flags.
        reserve = set()
        for sid, slot in slot_index.items():
            if day_key(slot.get('day')) in elective_days:
                lo, hi = hm(slot.get('start_time')), hm(slot.get('end_time'))
                # Time_Slots end may be before the inclusive end of reservation.
                if lo < end and hi > start:
                    reserve.add(sid)
        cfg.reserved_slot_ids = frozenset(reserve)
        flagged = {sid for sid, slot in slot_index.items() if txt(slot.get('elective_reserved_sem4plus')).lower() in ('yes','true','1')}
        if flagged != reserve:
            # Window flags are advisory; mode processing below determines
            # whether the reservation is active at all.
            window_flag_mismatches = sorted(flagged ^ reserve)
        else:
            window_flag_mismatches = []

    def parse_window_mode(raw):
        mode = raw.strip().upper()
        allowed = {'NO_CLASSES_ALLOWED', 'REGULAR_CLASSES_BLOCKED', 'DISABLED'}
        if mode not in allowed:
            raise PolicyError('Unknown Open Elective mode. Choose exactly one of: ' + ', '.join(sorted(allowed)))
        return mode, (
            'NO_CLASSES_ALLOWED: block all teaching sessions; '
            'REGULAR_CLASSES_BLOCKED: permit only explicitly listed course IDs; '
            'DISABLED: no reservation. Mode selected: ' + mode
        )

    requested_mode = handle('open_elective_window_mode', 'APPLIED', parse_window_mode)
    if requested_mode is None:
        if 'open_elective_window_mode' in active:
            # INVALID is already recorded; never silently fall back to another mode.
            cfg.elective_window_mode = 'INVALID'
        elif all(name in active for name in electoral):
            # Preserve behavior of existing V8 workbooks before adding P31.
            cfg.elective_window_mode = 'NO_CLASSES_ALLOWED'
            cfg.warnings.append('open_elective_window_mode absent: legacy NO_CLASSES_ALLOWED assumed. Add an explicit policy row to make intent portable.')
        else:
            cfg.elective_window_mode = 'DISABLED'
    else:
        cfg.elective_window_mode = requested_mode

    if cfg.elective_window_mode not in ('DISABLED', 'INVALID') and not all(name in active for name in electoral):
        cfg.errors.append('Open Elective mode requires P02-P05: days, start, end and minimum eligible semester.')
    if cfg.elective_window_mode == 'DISABLED':
        cfg.reserved_slot_ids = frozenset()
        if all(name in active for name in electoral):
            cfg.warnings.append('Open Elective mode is DISABLED; the configured days/times are informational only and do not reserve teaching slots.')
    elif cfg.elective_window_mode in ('NO_CLASSES_ALLOWED','REGULAR_CLASSES_BLOCKED'):
        if 'window_flag_mismatches' in locals() and window_flag_mismatches:
            cfg.warnings.append('Time_Slots elective_reserved_sem4plus flags disagree with the active Institution_Policies window. Policy times take precedence. Mismatches: '+str(window_flag_mismatches[:30]))

    ltp_rules = handle('language_ltp_controls_session_counts','DATA_CHECKED',
                      lambda raw: (_parse_course_spec(raw), 'Course codes and L-T-P checked against Courses; Course_Assignments selects actual groups.')) or {}
    for cid, target in ltp_rules.items():
        if cid not in courses:
            cfg.errors.append(f'Language L-T-P policy references missing course {cid}')
        elif ''.join(re.findall(r'\d',txt(courses[cid].get('L-T-P')))) != ''.join(re.findall(r'\d',target)):
            cfg.errors.append(f'{cid} L-T-P differs: policy {target}, Courses {courses[cid].get("L-T-P")}')
    def parse_explicit_elective_courses(raw):
        explicit = frozenset(x.strip() for x in raw.split(',') if x.strip())
        if not explicit:
            raise PolicyError('Supply one or more actual Open Elective course IDs (comma-separated).')
        unknown = sorted(set(explicit) - set(courses))
        if unknown:
            raise PolicyError('Unknown Open Elective course IDs in Courses: ' + ', '.join(unknown))
        return explicit, 'Actual Open Elective course IDs read explicitly from the workbook: ' + ', '.join(sorted(explicit))

    explicit_codes = handle('elective_course_codes','DATA_CHECKED', parse_explicit_elective_courses)
    cfg.window_allowed_course_codes = frozenset(explicit_codes or ())
    if cfg.elective_window_mode == 'REGULAR_CLASSES_BLOCKED' and not cfg.window_allowed_course_codes:
        cfg.errors.append('REGULAR_CLASSES_BLOCKED requires an enabled elective_course_codes policy listing real course IDs. This is never inferred from course_type.')
    # Course types and language L-T-P are for reporting, never for exemptions.
    # Actual window permission comes EXCLUSIVELY from elective_course_codes.
    def is_open_elective_type(value):
        label = ' '.join(txt(value).lower().replace('-', ' ').split())
        return label in ('fle', 'open elective', 'foreign language elective',
                         'language elective', 'open electives')

    inferred = {cid for cid, row in courses.items()
                if is_open_elective_type(row.get('course_type'))}
    cfg.elective_courses = frozenset(set(ltp_rules) | set(cfg.window_allowed_course_codes) | inferred)
    if cfg.reserved_slot_ids:
        not_open = sorted(cid for cid, row in courses.items()
                          if 'elective' in txt(row.get('course_type')).lower()
                          and cid not in cfg.elective_courses)
        if not_open:
            cfg.warnings.append('Program/unspecified elective categories are '
                                'separate from language electives: '+', '.join(not_open)+
                                '. Actual window permissions depend on open_elective_window_mode and elective_course_codes.')
        if 'CLV200' in courses and 'CLV200' not in cfg.elective_courses:
            cfg.warnings.append('Value Addition is not automatically an Open Elective; only explicitly listed course IDs may be allowed by REGULAR_CLASSES_BLOCKED.')

    # A fixed non-teaching interval is deliberately NOT a new class or a new
    # scheduling slot. Its clock times are validated against all teachable slots
    # and rendered as a separate row. This avoids hardcoding university names.
    def parse_fixed_blocks(raw):
        blocks = []
        for clause in raw.split(';'):
            if not clause.strip():
                continue
            match = re.fullmatch(r'\s*([^@;]{2,64})@(\d{2}:\d{2})-(\d{2}:\d{2})\s*', clause)
            if not match:
                raise PolicyError('Use Label@HH:MM-HH:MM;Label@HH:MM-HH:MM')
            label, start, end = match.group(1).strip(), hm(match.group(2)), hm(match.group(3))
            if not 0 <= start < end <= 1440:
                raise PolicyError(f'Invalid fixed block time range: {clause!r}')
            for other_name, other_start, other_end in blocks:
                if start < other_end and other_start < end:
                    raise PolicyError(f'Fixed blocks overlap: {label} and {other_name}')
            for slot in slots:
                if flag(slot.get('academic_classes_allowed')):
                    lo, hi = hm(slot.get('start_time')), hm(slot.get('end_time'))
                    if start < hi and lo < end:
                        raise PolicyError(f'{label} overlaps teachable Time_Slots {slot.get("slot_id")}; '
                                          'remove the overlap from the workbook calendar first')
            blocks.append((label, start, end))
        if not blocks:
            raise PolicyError('Define at least one fixed block')
        return tuple(sorted(blocks, key=lambda x:x[1])), (
            'Validated non-teaching clock blocks outside every teaching Time_Slot. '
            'Shown in section/faculty reports; no mentor teaching session is fabricated.')
    fixed_blocks = handle('fixed_non_teaching_blocks', 'APPLIED', parse_fixed_blocks)
    if fixed_blocks is not None:
        cfg.fixed_non_teaching_blocks = fixed_blocks

    practical_minutes = handle('practical_duration_exact_minutes', 'APPLIED',
                               lambda raw: (number(raw,'practical duration minutes',1),
                               'Every practical session must occupy precisely this many continuous clock minutes. '
                               'Enforced when selecting candidates and independently validated.'))
    if practical_minutes is not None:
        cfg.practical_exact_minutes = practical_minutes

    # A fixed lunch is supported ONLY when the workbook explicitly defines
    # its clock interval using the independently validated P41 block policy.
    # This prevents a misleading APPLIED/false audit row and, importantly,
    # prevents declaring a lunch rule enforced without a real protected gap.
    if 'fixed_lunch_slot_per_section' in active:
        name = 'fixed_lunch_slot_per_section'
        try:
            fixed_lunch_requested = flag(get(name))
            if not fixed_lunch_requested:
                cfg.note(name, 'DISABLED',
                         'Fixed lunch rule is off. If a lunch block is defined by '
                         'fixed_non_teaching_blocks, that separate policy protects it.')
            else:
                if 'fixed_non_teaching_blocks' not in active:
                    raise PolicyError('Fixed lunch requires an enabled '
                                      'fixed_non_teaching_blocks policy giving the lunch clock times.')
                lunch_blocks = [(label, lo, hi) for label, lo, hi in cfg.fixed_non_teaching_blocks
                                if re.search(r'\blunch\b', label, re.IGNORECASE)]
                if len(lunch_blocks) != 1:
                    raise PolicyError('Fixed lunch requires exactly one validated Lunch block '
                                      'in fixed_non_teaching_blocks (e.g. Lunch Break@13:25-14:25).')
                _, start, end = lunch_blocks[0]
                cfg.note(name, 'APPLIED',
                         f'Fixed lunch {start//60:02}:{start%60:02}-{end//60:02}:{end%60:02} '
                         'is protected every teaching day by the enabled '
                         'fixed_non_teaching_blocks policy: no teaching Time_Slot '
                         'overlaps this interval; the timetable shows Lunch Break.')
        except PolicyError as ex:
            cfg.errors.append(f'{active[name].get("policy_id")} ({name}): {ex}')
            cfg.note(name, 'INVALID', str(ex))
    def parse_lunch(raw):
        m = re.fullmatch(r'ONE_FREE_FROM_PERIOD_([0-9_]+)', raw.upper())
        if not m:
            raise PolicyError('Supported syntax: ONE_FREE_FROM_PERIOD_04_05_06')
        ps = tuple(int(p) for p in m.group(1).split('_'))
        if len(set(ps)) != len(ps) or len(ps) < 2:
            raise PolicyError('Lunch requires 2+ distinct period numbers')
        if not all(any(p in q for q in periods.values()) for p in ps):
            raise PolicyError(f'Lunch period outside Time_Slots: {ps}')
        return ps, f'Each parent section and overlapping mixed cohort has at least 1 free period among {ps} per day.'
    lunch = handle('flexible_lunch_one_free_midday_period','APPLIED', parse_lunch)
    if lunch is not None:
        cfg.lunch_periods = tuple(lunch); cfg.lunch_free_count=1

    freq = handle('mentoring_frequency_weeks','APPLIED',lambda raw:(number(raw,'mentoring frequency',1),'Mentor sessions are generated for selected mentor week (or omitted in other weeks).'))
    if freq is not None:
        cfg.mentor_weeks = freq
        cfg.mentor_courses = frozenset(cid for cid, row in courses.items() if 'mentor' in txt(row.get('course_name')).lower() and re.findall(r'\d+',txt(row.get('L-T-P'))) == ['0','0','0'])
        if not cfg.mentor_courses:
            cfg.errors.append('Mentoring policy enabled but no zero-L-T-P course containing Mentor in its course name.')
        if freq > 2:
            cfg.warnings.append('Mentoring is represented by selected week only; use coordinator controls for other weeks in a multiweek cycle.')

    duration = handle('lab_contiguous_periods','APPLIED',lambda raw:(number(raw,'lab contiguous periods',1),'P=2 practical occupies one continuous two-period block from Courses L-T-P.'))
    if duration is not None:
        cfg.practical_length = duration
        if duration != 2 and any(re.findall(r'\d+',txt(c.get('L-T-P')))[-1:] == ['2'] for c in courses.values()):
            cfg.errors.append('lab_contiguous_periods contradicts Courses with P=2. The L-T-P session length remains authoritative.')
    bool_rule('lab_same_room_entire_session','One room is reserved throughout each multi-period session.',must_true=True)
    def batch_rule(raw):
        if raw.upper() != 'DEFAULT_TWO_UNLESS_WHOLE_CLASS':
            raise PolicyError('Unsupported batch-default mode. Provide explicit group IDs in Course_Assignments.')
        return raw,'Existing LAB_BATCH/whole-class assignments determine required sessions; no rows are invented.'
    handle('lab_batches_per_section','DATA_CHECKED',batch_rule)
    for name,description in [
        ('prevent_teacher_overlap','Every teacher is at most in one class per slot.'),
        ('prevent_room_overlap','Each assigned room is exclusive per slot.'),
        ('prevent_student_group_overlap','Same/overlapping student groups may not clash.'),
        ('require_room_capacity','Room seats must be >= group student count.')]:
        bool_rule(name,description,must_true=True)

    daily = handle('faculty_max_daily_sessions','APPLIED',lambda raw:(number(raw,'faculty daily max',1),'Global maximum: combined with individual Faculty sheet limits (stricter wins).'))
    weekly = handle('faculty_max_weekly_contact_hours','APPLIED',lambda raw:(number(raw,'faculty weekly clock hours',1),'Global clock-hour cap, combined with Faculty sheet limits.'))
    consecutive = handle('faculty_max_consecutive_slots','APPLIED',lambda raw:(number(raw,'max consecutive periods',1),'Global cap, combined with Faculty sheet limits.'))
    cfg.faculty_daily_cap, cfg.faculty_weekly_hours_cap, cfg.faculty_consecutive_cap = daily, weekly, consecutive

    stg = bool_rule('minimize_student_gaps','Contributes section-gap objective in fairness mode.')
    cfg.fairness_gap_enabled = bool(stg)
    if stg is not None and not optimize_fairness:
        for item in cfg.audit:
            if item['policy_name'] == 'minimize_student_gaps':
                item['status'] = 'CONDITIONAL'
                item['implementation'] = 'Enabled in Excel; inactive for this standard CP-SAT run. Activate fairness mode to include student gaps.'
    undesirable = bool_rule('balance_undesirable_slots','Applies soft preference for periods in the last two available periods of each day.')
    if undesirable:
        cfg.late_weight=1
        cfg.late_slot_ids = frozenset(sid for sid, row in slot_index.items()
            if number(row.get('period'),'period',1) in periods[day_key(row.get('day'))][-2:])
    lang = bool_rule('language_enrollee_no_other_class_overlap','Uses group membership and shared theory links from Student_Groups.')
    cfg.language_conflicts=bool(lang)

    for name in ('mandarin_two_groups_shared_theory','german_two_groups_share_theory','shared_theory_course_structure'):
        if name not in active:
            continue
        def check_shared(raw):
            cid, practical_n, theory_n = _parse_shared(raw)
            if cid not in courses:
                raise PolicyError(f'Unknown course {cid}')
            practical_groups = {txt(a.get('group_id')) for a in assignments if txt(a.get('course_id'))==cid
                                and 'PRACTICAL' in txt(a.get('scheduling_component')) and txt(a.get('scheduling_component'))!='DO_NOT_SCHEDULE'
                                and txt(a.get('practical_blocks_week')) not in ('0','')}
            theoretical_groups = {txt(a.get('group_id')) for a in assignments if txt(a.get('course_id'))==cid
                                  and txt(a.get('scheduling_component')) in ('THEORY_ONLY','THEORY_AND_PRACTICAL')}
            if len(practical_groups)!=practical_n or len(theoretical_groups)!=theory_n:
                raise PolicyError(f'{cid} expected {practical_n} practical groups and {theory_n} theory groups, but found {len(practical_groups)} and {len(theoretical_groups)}.')
            for gid in practical_groups:
                target=txt(groups.get(gid,{}).get('theory_delivery_group_id'))
                if target and target not in theoretical_groups:
                    raise PolicyError(f'{gid} refers to unassigned theory group {target}')
            return cid, f'Confirmed {practical_n} separate practical groups / {theory_n} shared theory groups in Course_Assignments and Student_Groups.'
        handle(name,'DATA_CHECKED',check_shared)

    def room_mode(raw):
        if raw.upper()!='ROOM_TYPE_AND_CAPACITY_ONLY':
            raise PolicyError('Only ROOM_TYPE_AND_CAPACITY_ONLY is implemented; facility/equipment constraints need a new model.')
        return raw,'Room type and capacity are checked; room facilities are NOT independently modeled.'
    handle('room_facilities_prototype_mode','DATA_CHECKED',room_mode)
    def scope(raw):
        if not raw.upper().startswith('ENABLE_WHOLE_SECTION_FALLBACKS'):
            raise PolicyError('Unsupported practical fallback switch; prototype assignments must be explicitly documented.')
        return raw,'Authorizes pre-labeled prototype assignments only; no teacher, session, or batch is invented by the solver.'
    handle('unverified_practical_scope_prototype_switch','DATA_CHECKED',scope)
    def lang_provisional(raw):
        if raw.upper()!='ALLOW_ONLY_WITH_SYNTHETIC_FLAG':
            raise PolicyError('Unsupported language provisional mode; use ALLOW_ONLY_WITH_SYNTHETIC_FLAG.')
        return raw,'Provisional language mappings remain marked in Course_Assignments and timetable CSV; not treated as official data.'
    handle('unverified_language_teacher_and_pairing_prototype','DATA_CHECKED',lang_provisional)
    def room_col(raw):
        if raw.upper()!='COURSES!L:M':
            raise PolicyError('Expected Courses!L:M (theory_room_type / practical_room_type columns).')
        missing=[c for c in courses.values() if not txt(c.get('theory_room_type')) or not txt(c.get('practical_room_type'))]
        if missing:
            raise PolicyError(f'{len(missing)} Courses rows missing room-type columns.')
        return raw,'Reads theory_room_type and practical_room_type from Courses for every session.'
    handle('course_room_type_by_session','DATA_CHECKED',room_col)
    def room_practical(raw):
        m=re.fullmatch(r'\s*([A-Za-z0-9_,\s]+):\s*P=2\s*=\s*2\s*contiguous\s*periods\s*',raw,re.I)
        if not m:
            raise PolicyError('Expected COURSE1,COURSE2: P=2 = 2 contiguous periods.')
        for cid in m.group(1).split(','):
            cid=cid.strip()
            row=courses.get(cid)
            if not row or not txt(row.get('L-T-P')).endswith('-2') or txt(row.get('practical_room_type')).lower()!='classroom':
                raise PolicyError(f'{cid} must exist with P=2 and practical_room_type=Classroom.')
        return raw,'Continuous classroom practical sessions get a classroom for the full block; no class may overlap the room.'
    handle('classroom_practical_exclusive_block','DATA_CHECKED',room_practical)

    # All enabled policy names must be interpreted or explicitly rejected.
    audited = {x['policy_name'] for x in cfg.audit}
    for name in cfg.active:
        if name not in audited:
            cfg.errors.append(f'Enabled policy {name} was not connected to an implementation.')
            cfg.note(name,'UNSUPPORTED','Enabled policy not implemented.')

    # Generic cohort membership: every non-PARENT, non-LAB_BATCH cohort that may
    # overlap original sections must declare its section mapping or a parent.
    # A legacy combined cohort has no explicit mapping in this workbook; it is
    # retained as an explicit *limitation* until the supplied sheet is corrected.
    for gid, row in groups.items():
        kind=txt(row.get('group_kind'))
        if kind.startswith('MIXED_') or kind in ('SHARED_LANGUAGE_THEORY','SHARED_VALUE_ADDITION_COHORT'):
            if not txt(row.get('synthetic_membership_by_original_section')) and not txt(row.get('parent_group_id')):
                cfg.warnings.append(f'Group {gid} has no explicit original-section membership. It cannot be considered verified for cross-section overlap without a mapping.')
    return cfg
