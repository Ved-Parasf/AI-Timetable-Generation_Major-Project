"""Grounded, read-only timetable coordinator assistant (V2).

No paid API or LLM; every factual answer is computed from the generated CSV,
uploaded Excel inventory, or independent validation report. It cannot modify
schedule data or prove student-ID-level elective conflicts.
"""
from __future__ import annotations

import re
from collections import defaultdict

from timetable_policies import load_policies

DAY_NAMES = {'MON': 'Monday', 'TUE': 'Tuesday', 'WED': 'Wednesday', 'THU': 'Thursday', 'FRI': 'Friday', 'SAT': 'Saturday', 'SUN':'Sunday'}
DAY_ALIASES = {'monday': 'MON', 'mon': 'MON', 'tuesday': 'TUE', 'tue': 'TUE',
               'wednesday': 'WED', 'wed': 'WED', 'thursday': 'THU', 'thu': 'THU',
               'friday': 'FRI', 'fri': 'FRI', 'saturday': 'SAT', 'sat': 'SAT', 'sunday':'SUN', 'sun':'SUN'}


def norm(value):
    return str(value if value is not None else '').strip()


def normalized(value):
    return re.sub(r'[^a-z0-9]+', ' ', norm(value).lower()).strip()


def _weekday_token(word):
    """Resolve exact weekdays or a unique, close misspelling of a full day name.

    Never fuzzy-match the short abbreviations: 'thu' and 'tue' must not be
    confused. Using only a unique match avoids guessing a requested day.
    """
    token = word.lower()
    if token in DAY_ALIASES:
        return DAY_ALIASES[token]
    if len(token) < 7 or not token.isalpha():
        return None
    candidates = [( _edit_distance(token, full), DAY_ALIASES[full])
                  for full in DAY_ALIASES if len(full) >= 6]
    if not candidates:
        return None
    best = min(distance for distance, _ in candidates)
    unique = {day for distance, day in candidates if distance == best}
    return next(iter(unique)) if best <= 2 and len(unique) == 1 else None


def parse_day(q):
    # Prefer a correctly typed weekday, then a uniquely correctable typo.
    words = re.findall(r'\b[a-zA-Z]+\b', q)
    for word in words:
        if word.lower() in DAY_ALIASES:
            return DAY_ALIASES[word.lower()]
    for word in words:
        matched = _weekday_token(word)
        if matched:
            return matched
    return None


def parse_period(q):
    m = re.search(r'\b(?:period|p)\s*(?:no\.?\s*)?(\d{1,2})\b', q.lower())
    return int(m.group(1)) if m else None


def match_value(q, values):
    """Find an exact normalized identifier/name, avoiding substring false matches."""
    query = normalized(q)
    options = sorted({norm(v) for v in values if norm(v)}, key=lambda x: (-len(x), x))
    for item in options:
        key = normalized(item)
        if key and re.search(r'(?<!\w)' + re.escape(key) + r'(?!\w)', query):
            return item
    # Accept a shortened *unique* teacher/room name only if no full match exists.
    return None


def match_course_reference(question, courses, rows=()):
    """Match a unique existing course by code *or* full course title.

    Accepts spaces/hyphens between course prefix and digits ("CLV 200", "CSL-240")
    without inventing a code. Identical course titles with different codes
    remain ambiguous until a code or section is supplied.
    Returns (course_code_or_None, candidate_codes_if_ambiguous).
    """
    catalog = {norm(code): norm(record.get('course_name'))
               for code, record in courses.items() if norm(code)}
    for row in rows:
        code = norm(row.get('course_id'))
        if code and code not in catalog:
            catalog[code] = norm(row.get('course_name'))
    by_code = []
    for code in catalog:
        match = re.fullmatch(r'([a-zA-Z]{2,6})([0-9]{2,5}[a-zA-Z]?)', code)
        if match:
            prefix, digits = match.groups()
            pattern = (r'(?<![a-zA-Z0-9])' + re.escape(prefix) + r'[\s_.-]*' + r'[\s_.-]*'.join(re.escape(char) for char in digits) + r'(?![a-zA-Z0-9])')
        else:
            pattern = r'(?<![a-zA-Z0-9])' + re.escape(code) + r'(?![a-zA-Z0-9])'
        if re.search(pattern, question, flags=re.IGNORECASE):
            by_code.append(code)
    if len(by_code) == 1:
        return by_code[0], ()
    if len(by_code) > 1:
        return None, tuple(sorted(by_code))

    # Require a full title, not a substring such as "Data" from "Data Mining".
    query = normalized(question)
    title_hits = [(code, normalized(title)) for code, title in catalog.items()
                  if title and len(normalized(title)) >= 4 and
                  re.search(r'(?<!\w)' + re.escape(normalized(title)) + r'(?!\w)', query)]
    if not title_hits:
        return None, ()
    longest = max(len(title) for _, title in title_hits)
    candidates = sorted({code for code, title in title_hits if len(title) == longest})
    return (candidates[0], ()) if len(candidates) == 1 else (None, tuple(candidates))


def _edit_distance(left, right):
    """Small deterministic spelling tolerance; no outside AI service required."""
    previous = list(range(len(right) + 1))
    for i, letter in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (letter != other)))
        previous = current
    return previous[-1]


def match_teacher(question, names):
    """Match a full faculty name, tolerating one typo in a name component.

    Fuzzy matches MUST be unique. Never silently select between similarly
    spelled faculty members or guess from a single first name.
    Returns (matching faculty name, is_fuzzy_match).
    """
    exact = match_value(question, names)
    if exact:
        return exact, False
    words = normalized(question).split()
    candidates = []
    for full_name in sorted({norm(n) for n in names if norm(n)}):
        parts = normalized(full_name).split()
        if len(parts) < 2 or len(words) < len(parts):
            continue
        for offset in range(len(words) - len(parts) + 1):
            window = words[offset:offset + len(parts)]
            distances = [_edit_distance(a, b) for a, b in zip(parts, window)]
            # Strict component-by-component typo tolerance.  This does not
            # incorrectly match e.g. Jyoti Sharma to Jyoti Singhal.
            if all(d <= (1 if len(part) >= 4 else 0)
                   for part, d in zip(parts, distances)) and 1 <= sum(distances) <= 2:
                candidates.append((sum(distances), full_name))
    if candidates:
        best = min(score for score, _ in candidates)
        winners = {name for score, name in candidates if score == best}
        if len(winners) != 1:
            return None, False
        return next(iter(winners)), True

    # Faculty may be addressed by a first name and an honorific: "Ashima
    # ma'am", "Swati madam", etc. Resolve ONLY an unambiguous exact given
    # name in the current faculty inventory, never infer a surname or choose
    # between two faculty members who share that first name. A standalone
    # given name is also a valid clarification reply after a question.
    # Full-name and exact teacher matching above always have precedence.
    # A bare first name during an availability query is too ambiguous in
    # institutional data. Only resolve one when the user also supplies a
    # faculty honorific or explicitly asks about teaching/courses.
    context = normalized(question)
    if not re.search(r'\b(maam|mam|madam|sir|professor|prof|taught|teach|teaches|teaching|subjects?|courses?|handles?)\b', context):
        return None, False
    unique_first_names = {}
    for full_name in sorted({norm(n) for n in names if norm(n)}):
        given_name = normalized(full_name).split()[0]
        if len(given_name) >= 4:
            unique_first_names.setdefault(given_name, set()).add(full_name)
    for given_name, matches in unique_first_names.items():
        if re.search(r'(?<!\w)' + re.escape(given_name) + r'(?!\w)', normalized(question)):
            if len(matches) == 1:
                return next(iter(matches)), True
            # An exact but ambiguous given name must never be guessed.
            return None, False
    return None, False


def parse_clock_minutes(question):
    """Understand 11:30 am, 11.30 AM, 14:30, and 9 am. Return minutes."""
    q = question.lower()
    with_minutes = re.search(
        r'(?<!\w)(\d{1,2})[:.](\d{2})\s*(a\.?m\.?|p\.?m\.?)?(?!\w)', q)
    without_minutes = None
    if not with_minutes:
        without_minutes = re.search(r'(?<!\w)(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?!\w)', q)
    if not with_minutes and not without_minutes:
        return None
    result = with_minutes or without_minutes
    hour = int(result.group(1))
    minute = int(result.group(2)) if with_minutes else 0
    period_suffix = (result.group(3) if with_minutes else result.group(2)) or ''
    suffix = period_suffix.replace('.', '').strip()
    if minute > 59:
        return None
    if suffix:
        if hour < 1 or hour > 12:
            return None
        hour = (hour % 12) + (12 if suffix.startswith('p') else 0)
    elif hour > 23:
        return None
    return 60 * hour + minute


def _clock_label(minutes):
    hour, minute = divmod(minutes, 60)
    return f'{(hour - 1) % 12 + 1}:{minute:02d} {"AM" if hour < 12 else "PM"}'


def _slot_for_clock_time(data, day, minutes):
    """Resolve the institution-specific period from its Excel Time_Slots."""
    matches = []
    for slot in data.get('Time_Slots', []):
        if norm(slot.get('day')).upper() != day:
            continue
        start = re.fullmatch(r'(\d{1,2}):(\d{2})', norm(slot.get('start_time')))
        end = re.fullmatch(r'(\d{1,2}):(\d{2})', norm(slot.get('end_time')))
        if not (start and end):
            continue
        a = int(start.group(1)) * 60 + int(start.group(2))
        b = int(end.group(1)) * 60 + int(end.group(2))
        if a <= minutes < b:
            matches.append(slot)
    return matches[0] if len(matches) == 1 else None



def _mentioned_day_segments(question):
    """Separate the slots named in a multi-day request, including weekday typos."""
    matches = [(m, _weekday_token(m.group()))
               for m in re.finditer(r'\b[a-zA-Z]+\b', question)]
    matches = [(m, code) for m, code in matches if code]
    return [(code, question[m.start(): matches[i + 1][0].start()
                            if i + 1 < len(matches) else len(question)])
            for i, (m, code) in enumerate(matches)]


def _teacher_slot_status(teacher, day, period, rows, data, faculty, *, clock_minutes=None):
    """Short factual Yes/No answer for a single faculty period.

    A teacher is free only when no scheduled class occupies the period AND
    the Excel availability record permits it. Unknown records remain unknown.
    """
    label = DAY_NAMES.get(day, day)
    slot = next((s for s in data.get('Time_Slots', [])
                 if norm(s.get('day')).upper() == day
                 and int_value(s.get('period')) == period), None)
    if slot is None:
        return (f'**I cannot confirm availability** for {teacher} on {label}, '
                f'Period {period}: this period is not defined in the timetable.'), 'UNKNOWN'

    start = _slot_time_minutes(norm(slot.get('start_time')))
    end = _slot_time_minutes(norm(slot.get('end_time')))
    if start is not None and end is not None:
        time_text = f' ({_clock_label(start)}–{_clock_label(end)})'
    elif clock_minutes is not None:
        time_text = f' ({_clock_label(clock_minutes)})'
    else:
        time_text = ''
    when = f'{label}, Period {period}{time_text}'

    scheduled = [r for r in rows if norm(r.get('faculty_name')) == teacher
                 and norm(r.get('day')).upper() == day
                 and int_value(r.get('period')) == period]
    if scheduled:
        # Do not repeat a practical that occupies two consecutive periods.
        descriptions = []
        for item in _merge_sessions(_whole_blocks(rows, scheduled)):
            group_id = norm(item.get('group_id'))
            section = _readable_section(group_id)
            course = norm(item.get('course_name')) or norm(item.get('course_id'))
            code = norm(item.get('course_id'))
            subject = f'{course} ({code})' if code and code.lower() not in course.lower() else course
            description = f'**{subject}** for **{section}**' if section else f'**{subject}**'
            if description not in descriptions:
                descriptions.append(description)
        detail = '; '.join(descriptions)
        return (f'**No — {teacher} is teaching** on {when}. '
                + (detail + '.' if detail else '')), 'BUSY'

    record = next((r for r in faculty.values() if norm(r.get('faculty_name')) == teacher), None)
    fid = norm(record.get('faculty_id')) if record else ''
    avail = next((a for a in data.get('Faculty_Availability', [])
                  if fid and norm(a.get('faculty_id')) == fid
                  and norm(a.get('slot_id')) == norm(slot.get('slot_id'))), None)
    if not avail:
        return (f'**I cannot confirm** whether {teacher} is free on {when}. '
                'Faculty availability has not been provided for this period.'), 'UNKNOWN'
    declared = norm(avail.get('is_available')).lower()
    if declared in ('no', 'false', '0', 'n'):
        return f'**No — {teacher} is unavailable** on {when}.', 'BLOCKED'
    if declared in ('yes', 'true', '1', 'y'):
        return f'**Yes — {teacher} is free** on {when}.', 'AVAILABLE'
    return (f'**I cannot confirm** whether {teacher} is free on {when}. '
            'The faculty availability value is not recognized.'), 'UNKNOWN'


def _teacher_availability_provenance(teacher, day, period, data, faculty):
    """Show original source_type on demand, not in every availability reply."""
    record = next((r for r in faculty.values() if norm(r.get('faculty_name')) == teacher), None)
    fid = norm(record.get('faculty_id')) if record else ''
    slot = next((s for s in data.get('Time_Slots', []) if norm(s.get('day')).upper() == day
                 and int_value(s.get('period')) == period), None)
    if not fid or not slot:
        return 'The faculty or time slot has no matching source record in the workbook.'
    entry = next((a for a in data.get('Faculty_Availability', [])
                  if norm(a.get('faculty_id')) == fid
                  and norm(a.get('slot_id')) == norm(slot.get('slot_id'))), None)
    if entry is None:
        return 'There is no matching faculty availability record in the workbook.'
    source = norm(entry.get('source_type')) or 'UNSPECIFIED'
    source_text = ('**Synthetic prototype assumption**, not independently verified real-world '
                   'faculty availability.' if source.upper() == 'SYNTHETIC'
                   else f'**{source}** (the provenance label recorded in Excel; independently unverified).')
    return (f'**Availability data source — {teacher} / {DAY_NAMES.get(day, day)}, '
            f'Period {period}**\n\nWorkbook Faculty_Availability.source_type: '
            f'**{source}**.\n\nInterpretation: {source_text}')


def _readable_section(group_id):
    """Preserve the named academic section and display practical-batch suffixes."""
    group = norm(group_id)
    if '__' not in group:
        return group or 'Unspecified student group'
    parent, batch = group.split('__', 1)
    return f'{parent} ({batch.replace("_", "-")})'


def _busy_group_summaries(teacher, day, rows):
    """Group consecutive period rows into teaching sessions, identifying sections."""
    by_class = defaultdict(set)
    for row in rows:
        if norm(row.get('faculty_name')) != teacher or norm(row.get('day')).upper() != day:
            continue
        period = int_value(row.get('period'))
        if not period:
            continue
        key = (norm(row.get('session_id')) or (
            norm(row.get('group_id')) + '/' + norm(row.get('course_id'))),
               norm(row.get('group_id')), norm(row.get('course_name')))
        by_class[key].add(period)
    result = []
    for (_sid, group_id, course_name), periods in sorted(by_class.items(), key=lambda item: min(item[1])):
        ordered = sorted(periods)
        formatted = (f'Periods {ordered[0]}–{ordered[-1]}' if len(ordered) > 1
                     and ordered == list(range(ordered[0], ordered[-1] + 1))
                     else 'Period ' + ', '.join(str(v) for v in ordered))
        section = _readable_section(group_id)
        title = f' — {course_name}' if course_name else ''
        result.append(f'- **{formatted}:** Teaching **{section}**{title}')
    return result


def _teacher_day_status(teacher, day, rows, data, faculty, *, meeting=False):
    """Lead with free periods and identify occupied periods by academic section.

    This uses the currently selected generated CSV and the workbook availability
    restrictions. Avoid the previous eight-row dump when five periods are free.
    """
    label = DAY_NAMES.get(day, day)
    slots = sorted((slot for slot in data.get('Time_Slots', [])
                    if norm(slot.get('day')).upper() == day),
                   key=lambda slot: int_value(slot.get('period')))
    if not slots:
        return f'No timetable periods are defined for {label}.'
    free, blocked, unknown = [], [], []
    for slot in slots:
        period = int_value(slot.get('period'))
        _, state = _teacher_slot_status(teacher, day, period, rows, data, faculty)
        start = _slot_time_minutes(norm(slot.get('start_time')))
        end = _slot_time_minutes(norm(slot.get('end_time')))
        time_text = (f'{_clock_label(start)}–{_clock_label(end)}' if start is not None and end is not None
                     else f'{norm(slot.get("start_time"))}–{norm(slot.get("end_time"))}')
        if state == 'AVAILABLE':
            free.append(f'- **Period {period}:** {time_text}')
        elif state == 'BLOCKED':
            blocked.append(f'Period {period}')
        elif state == 'UNKNOWN':
            unknown.append(f'Period {period}')
    heading = ('available to meet' if meeting else 'available')
    result = [f'**{teacher} is {heading} on {label} during {len(free)} period(s):**',
              '\n'.join(free) if free else 'No available teaching periods found in the uploaded dataset.']
    busy = _busy_group_summaries(teacher, day, rows)
    extras = []
    if busy:
        extras.append('**Teaching assignments:**\n' + '\n'.join(busy))
    if blocked:
        extras.append(f'**Unavailable according to Excel:** {", ".join(blocked)}.')
    if unknown:
        extras.append(f'**Availability not confirmed:** {", ".join(unknown)}.')
    if extras:
        result.append('\n\n'.join(extras))
    return '\n\n'.join(result)


def _faculty_display_name(value):
    """Fix unmatched department parentheses for display only, never in data."""
    name = norm(value)
    matched = re.fullmatch(r'(.*?)\s*\(([A-Za-z]{2,12})', name)
    return (matched.group(1).strip() + ' (' + matched.group(2) + ')') if matched else name


def _course_teaching_assignments(course, rows, groups, *, teacher=None, include_sections=False):
    """Describe real course allocations in the SELECTED timetable CSV.

    Whole-class theory and lab batches are consolidated under the correct
    parent section. A two-period practical is never counted as two classes.
    Unknown/mixed group IDs remain explicitly named instead of inventing a
    parent section or individual student memberships.
    """
    matching = [row for row in rows
                if norm(row.get('course_id')) == course
                and (not teacher or norm(row.get('faculty_name')) == teacher)]
    assignments = defaultdict(lambda: defaultdict(lambda: {'kinds': set(), 'batches': set()}))
    for row in matching:
        name = norm(row.get('faculty_name'))
        gid = norm(row.get('group_id'))
        if not name or not gid:
            continue
        group_record = groups.get(gid, {})
        known_parent = norm(group_record.get('parent_group_id'))
        if known_parent in groups and norm(groups[known_parent].get('group_kind')).upper() == 'PARENT':
            section = known_parent
        elif '__' in gid and gid.split('__', 1)[0] in groups and norm(groups[gid.split('__', 1)[0]].get('group_kind')).upper() == 'PARENT':
            section = gid.split('__', 1)[0]
        else:
            section = gid  # Elective cohorts without known sections stay separate.
        item = assignments[name][section]
        kind = norm(row.get('kind')).replace('_', ' ').title() or 'Session'
        item['kinds'].add(kind)
        if section != gid:
            batch = gid.split('__', 1)[1].replace('_', '-') if '__' in gid else gid
            item['batches'].add(batch)

    if not assignments:
        return None

    if not include_sections:
        return '\n'.join(
            '- ' + _faculty_display_name(name) + ' — ' + ', '.join(sorted({kind for item in sections.values() for kind in item['kinds']}))
            for name, sections in sorted(assignments.items())
        )

    lines = []
    for name, sections in sorted(assignments.items()):
        details = []
        for section, info in sorted(sections.items()):
            kinds = ', '.join(sorted(info['kinds']))
            if info['batches']:
                kinds += ' (batches: ' + ', '.join(sorted(info['batches'])) + ')'
            details.append(f'**{section}** — {kinds}')
        lines.append(f'- **{_faculty_display_name(name)}**\n' + '\n'.join(f'  - {detail}' for detail in details))
    return '\n'.join(lines)



def _teacher_section_directory(teacher, rows, groups, *, day=None,
                               include_courses=False, include_batches=False):
    """Answer which *sections* a teacher handles, not their weekly timetable.

    Derive section IDs from the chosen timetable's group IDs and the workbook
    group hierarchy. Never treat a practical batch as a second parent section.
    """
    found = defaultdict(lambda: {'courses': set(), 'batches': set()})
    for row in rows:
        if norm(row.get('faculty_name')) != teacher:
            continue
        if day and norm(row.get('day')).upper() != day:
            continue
        group_id = norm(row.get('group_id'))
        if not group_id:
            continue
        record = groups.get(group_id, {})
        parent = norm(record.get('parent_group_id'))
        if parent in groups and norm(groups[parent].get('group_kind')).upper() == 'PARENT':
            section = parent
        elif '__' in group_id and group_id.split('__', 1)[0] in groups and \
                norm(groups[group_id.split('__', 1)[0]].get('group_kind')).upper() == 'PARENT':
            section = group_id.split('__', 1)[0]
        else:
            # Mixed elective cohorts are kept explicit instead of inventing
            # individual students or claiming a parent class section.
            section = group_id
        item = found[section]
        code = norm(row.get('course_id'))
        if code:
            item['courses'].add(code)
        if group_id != section:
            batch = group_id.split('__', 1)[-1].replace('_', '-') if '__' in group_id else group_id
            item['batches'].add(batch)
    label = DAY_NAMES.get(day, day) if day else None
    if not found:
        tail = f' on {label}' if label else ''
        return f'No scheduled sections found for **{teacher}**{tail}.'
    lines = []
    for section, info in sorted(found.items()):
        detail = []
        if include_courses and info['courses']:
            detail.append('courses: ' + ', '.join(sorted(info['courses'])))
        if include_batches and info['batches']:
            detail.append('batches: ' + ', '.join(sorted(info['batches'])))
        lines.append(f'- **{section}**' + (f' — {"; ".join(detail)}' if detail else ''))
    tail = f' on {label}' if label else ''
    noun = 'section' if len(found) == 1 else 'sections'
    return f'**{teacher} teaches {len(found)} {noun}{tail}:**\n\n' + '\n'.join(lines)


def _slot_time_minutes(value):
    m = re.fullmatch(r'(\d{1,2}):(\d{2})', value)
    if not m:
        return None
    hour, minute = map(int, m.groups())
    return (hour * 60 + minute) if hour < 24 and minute < 60 else None


def _teacher_multiple_slot_status(question, teacher, segments, rows, data, faculty):
    """Check all requested times, return one readable summary and each slot result."""
    details, statuses = [], []
    for day, portion in segments:
        minutes = parse_clock_minutes(portion)
        explicit_period = parse_period(portion)
        label = DAY_NAMES.get(day, day)
        if minutes is None and explicit_period is None:
            return (f'Please provide a time or period for {label}; '
                    'I cannot check all requested slots yet.')
        if minutes is not None:
            slot = _slot_for_clock_time(data, day, minutes)
            if slot is None:
                return f'{_clock_label(minutes)} on {label} is outside the teaching periods in Excel.'
            resolved = int_value(slot.get('period'))
            if explicit_period is not None and explicit_period != resolved:
                return (f'Your question mentions **Period {explicit_period}** but '
                        f'**{_clock_label(minutes)} on {label}** falls in **Period {resolved}**. '
                        'Please clarify.')
            period = resolved
        else:
            period = explicit_period
        _answer, status = _teacher_slot_status(teacher, day, period, rows, data, faculty,
                                               clock_minutes=minutes)
        statuses.append(status)
        where = f'{label}, Period {period}'
        if minutes is not None:
            where += f' ({_clock_label(minutes)})'
        if status == 'AVAILABLE':
            explanation = 'Available in the uploaded timetable and Excel availability data.'
        elif status == 'BLOCKED':
            explanation = 'Marked unavailable in Excel.'
        elif status == 'BUSY':
            assignments = _busy_group_summaries(teacher, day, [r for r in rows
                           if int_value(r.get('period')) == period])
            explanation = 'Teaching ' + ('; '.join(line.split('Teaching ', 1)[-1] for line in assignments)
                                         if assignments else 'another class') + '.'
        else:
            explanation = 'Availability cannot be confirmed.'
        details.append(f'- **{where}:** {explanation}')
    if all(status == 'AVAILABLE' for status in statuses):
        headline = f'**Yes — {teacher} is available at both requested slots.**' if len(statuses) == 2 else f'**{teacher} is available at all requested slots.**'
    elif any(status in ('BLOCKED', 'BUSY') for status in statuses):
        headline = f'**No — {teacher} is not available at every requested slot.**'
    else:
        headline = f'**Availability for {teacher} cannot be fully confirmed.**'
    return headline + '\n\n' + '\n'.join(details)


_ORDINAL_PERIODS = {
    'first': 1, 'second': 2, 'third': 3, 'fourth': 4,
    'fifth': 5, 'sixth': 6, 'seventh': 7, 'eighth': 8,
    '1st': 1, '2nd': 2, '3rd': 3, '4th': 4, '5th': 5,
    '6th': 6, '7th': 7, '8th': 8,
}


def _ordinal_period(question):
    match = re.search(
        r'\b(first|second|third|fourth|fifth|sixth|seventh|eighth|[1-8](?:st|nd|rd|th))'
        r'\s+(?:class|period|lecture|slot)\b', question, flags=re.IGNORECASE)
    return _ORDINAL_PERIODS.get(match.group(1).lower()) if match else None


def _mentioned_clock_times(question):
    """Parse every unambiguous clock expression, not just the first one."""
    pattern = r'(?<!\w)\d{1,2}[:.]\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?(?!\w)|(?<!\w)\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?)(?!\w)'
    values = []
    for match in re.finditer(pattern, question, re.IGNORECASE):
        minutes = parse_clock_minutes(match.group(0))
        if minutes is not None:
            values.append(minutes)
    return values


def _clock_from_field(value):
    text = norm(value)
    match = re.fullmatch(r'(\d{1,2}):(\d{2})', text)
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _two_period_teacher_check(question, teacher, day, rows, data, faculty):
    """Check consecutive faculty slots only; never claim complete lab feasibility.

    A 2-period practical may include the normal short transition between
    lessons; it must use two consecutive Excel periods on the same day.
    """
    label = DAY_NAMES.get(day, day)
    clocks = _mentioned_clock_times(question)
    if not clocks:
        return ('Please specify a start time or a period for the two-period practical. '
                'I have not made a scheduling change.')
    if len(clocks) > 2:
        return 'Please specify one start time and, optionally, one end time for the proposed practical.'
    start = clocks[0]
    slots = sorted((s for s in data.get('Time_Slots', [])
                    if norm(s.get('day')).upper() == day), key=lambda s: int_value(s.get('period')))
    first = next((i for i, slot in enumerate(slots)
                  if _clock_from_field(slot.get('start_time')) == start), None)
    if first is None:
        return (f'**{_clock_label(start)} on {label}** is not the start of a teaching period '
                'in the uploaded workbook. A practical must start at a defined period boundary.')
    if first + 1 >= len(slots):
        return f'There are not two teaching periods remaining on {label} from {_clock_label(start)}.'
    pair = slots[first:first + 2]
    periods = [int_value(slot.get('period')) for slot in pair]
    if periods[1] != periods[0] + 1:
        return 'The proposed practical does not fit into two consecutive teaching periods.'
    expected_end = _clock_from_field(pair[-1].get('end_time'))
    if len(clocks) == 2 and clocks[1] != expected_end:
        return (f'The requested end time **{_clock_label(clocks[1])}** does not align with '
                f'two consecutive periods starting at **{_clock_label(start)}**. '
                f'Those periods end at **{_clock_label(expected_end)}**.')
    checks = [_teacher_slot_status(teacher, day, period, rows, data, faculty)
              for period in periods]
    summary = (f'**Two-period practical availability — {teacher}**\n\n'
               f'{label}, Periods {periods[0]}–{periods[1]} '
               f'({_clock_label(start)}–{_clock_label(expected_end)})')
    statuses = [status for _, status in checks]
    if any(status in ('BUSY', 'BLOCKED') for status in statuses):
        conclusion = '**No — this teacher cannot be scheduled for that complete practical window under the uploaded data.**'
    elif all(status == 'AVAILABLE' for status in statuses):
        conclusion = ('**The teacher is available for both periods in the uploaded dataset.** '
                      'That alone does not establish that a practical can be scheduled.')
    else:
        conclusion = '**Teacher availability cannot be confirmed for both periods.**'
    return (summary + '\n\n' + conclusion + '\n\n'
            + '\n\n'.join(f'- Period {p}: {message}' for p, (message, _) in zip(periods, checks))
            + '\n\n**Additional checks still needed:** specific course/session, student group, '
              'eligible laboratory and capacity, institutional policies, and independent '
              'validation of a proposed timetable. No move has been made.')


def _teacher_free_periods(teacher, day, rows, data, faculty):
    """Show actual free periods first, even in conversational meeting questions."""
    return _teacher_day_status(teacher, day, rows, data, faculty, meeting=True)


def _proposed_class_answer(question, course, group, data, rows):
    """Explain necessary checks rather than treating a soft rule as full feasibility."""
    policies = load_policies(data)
    explicit_slots = sorted(policies.avoid_slot_ids)
    current = [r for r in rows if norm(r.get('course_id')) == course and
               (not group or _belongs(r, group,
                    {norm(g.get('group_id')):g for g in data.get('Student_Groups', [])}))]
    heading = f'**Proposed class placement — {course}' + (f' / {group}' if group else '') + '**'
    details = [heading, '**Not yet verified as feasible.** I need the specific session '
               '(theory or practical, and batch where relevant), its requested period(s) and a room.']
    if 'thursday afternoon' in question.lower():
        if {'THU_07', 'THU_08'} & set(explicit_slots):
            details.append('Thursday Periods 7–8 are **soft-avoid** slots in the uploaded policies, '
                           'so the policy alone does not prohibit teaching then. '
                           'However, it also does not guarantee a feasible placement.')
        else:
            details.append('The requested Thursday time must be checked against the '
                           'uploaded institution-specific time slots and policies.')
    if current:
        details.append(f'The current timetable contains {len(_merge_sessions(current))} '
                       'matching scheduled session(s); none has been moved.')
    details.append('To approve a change, check the teacher, relevant student groups, '
                   'room type/capacity, full duration and hard institutional constraints. '
                   'Use **Results → Coordinator workflows** to preview and independently '
                   'validate a specific move. No change has been made.')
    return '\n\n'.join(details)

def int_value(value, default=0):
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _session_groups(group_id, groups):
    """Original sections represented by a class, batch or mixed elective.

    Synthetic membership is section-level only. It is not individual enrolment.
    """
    record = groups.get(group_id, {})
    result = {group_id}
    parent = norm(record.get('parent_group_id'))
    if parent:
        result.add(parent)
    for member in norm(record.get('synthetic_membership_by_original_section')).split(';'):
        original = member.split('=')[0].strip()
        if original in groups:
            if norm(groups[original].get('group_kind')) == 'PARENT': result.add(original)
            elif norm(groups[original].get('group_kind')) == 'LAB_BATCH':
                parent = norm(groups[original].get('parent_group_id'))
                if parent in groups: result.add(parent)
    return result


def _belongs(row, requested, groups):
    gid = norm(row.get('group_id'))
    if gid == requested:
        return True
    # For parent sections, include their batches and relevant elective cohorts.
    if norm(groups.get(requested, {}).get('group_kind')) == 'PARENT':
        return requested in _session_groups(gid, groups)
    return False


def _group_match(question, groups, rows):
    all_ids = set(groups) | {norm(r.get('group_id')) for r in rows}
    return match_value(question, all_ids)


def _merge_sessions(rows):
    """Collapse two CSV period rows for one two-period practical."""
    sessions = defaultdict(list)
    for r in rows:
        sessions[norm(r.get('session_id'))].append(r)
    out = []
    for sid, records in sessions.items():
        ordered = sorted(records, key=lambda x: (norm(x.get('day')), int_value(x.get('period'))))
        base = dict(ordered[0])
        first_p = int_value(ordered[0].get('period'))
        last_p = int_value(ordered[-1].get('period'))
        if len(ordered) > 1 and first_p != last_p:
            base['display_period'] = f'{first_p}–{last_p}'
            base['end_time'] = norm(ordered[-1].get('end_time'))
        else:
            base['display_period'] = str(first_p)
        out.append(base)
    day_index = {day: ix for ix, day in enumerate(DAY_NAMES)}
    return sorted(out, key=lambda r: (day_index.get(norm(r.get('day')), 99),
                                      int_value(r.get('period')), norm(r.get('course_id')),
                                      norm(r.get('group_id'))))


def _whole_blocks(rows, matching_period_rows):
    """Restore both CSV rows of two-period practicals for a period-specific query."""
    session_ids = {norm(r.get('session_id')) for r in matching_period_rows}
    return [r for r in rows if norm(r.get('session_id')) in session_ids]


def _format_session(r):
    day = DAY_NAMES.get(norm(r.get('day')), norm(r.get('day')))
    kind = norm(r.get('kind')).replace('_', ' ').title()
    return (f'**{day}, Period {norm(r.get("display_period", r.get("period")))}** '
            f'({norm(r.get("start_time"))}–{norm(r.get("end_time"))}) — '
            f'**{norm(r.get("course_id"))} · {norm(r.get("course_name"))}** '
            f'({kind}); {norm(r.get("group_id"))}; '
            f'{norm(r.get("faculty_name"))}; room {norm(r.get("room_id"))}')


def _display_matches(result, title):
    merged = _merge_sessions(result)
    if not merged:
        return (f'No matching scheduled session was found for **{title}**. '
                'Try removing a day or period filter. The result only describes the generated timetable.')
    # Show an entire section's weekly timetable without silently discarding entries.
    limit = 120
    lines = [f'- {_format_session(row)}' for row in merged[:limit]]
    heading = f'**{title}** — {len(merged)} matching session(s)'
    note = ('\n\nThe list is limited to 120 entries. Specify a day, teacher, or section for the remainder.'
            if len(merged) > limit else '')
    return heading + '\n\n' + '\n'.join(lines) + note


def _format_room_list(room_ids, rooms):
    # Use all matching inventory entries, not an arbitrary first-35 cutoff.
    lines=[]
    for rid in sorted(room_ids):
        detail=rooms[rid]
        capacity=int_value(detail.get('capacity'))
        qualifier=' · synthetic prototype room' if rid.startswith('SYNTHETIC_LAB') else ''
        lines.append(f'{rid} ({capacity} seats{qualifier})')
    return ', '.join(lines) or 'None'


def _count_summary(rows):
    sessions = _merge_sessions(rows)
    return (f'**Timetable summary**\n\n'
            f'- Scheduled teaching sessions: **{len(sessions)}**\n'
            f'- Scheduled session-period rows: **{len(rows)}**\n'
            f'- Scheduled group IDs: **{len({norm(r.get("group_id")) for r in rows})}**\n'
            f'- Courses: **{len({norm(r.get("course_id")) for r in rows})}**\n'
            f'- Teaching faculty: **{len({norm(r.get("faculty_name")) for r in rows})}**\n\n'
            'These are counts from the generated CSV, not from external university records.')


def _validation_answer(report):
    if not report:
        return 'The independent validation report is unavailable. Generate a timetable and review the Validation & log tab.'
    metrics = report.get('metrics', {})
    errors = report.get('errors', [])
    message = (f'**Independent validation: {report.get("status", "UNKNOWN")}**\n\n'
               f'- Required sessions: **{metrics.get("expected_sessions", "not reported")}**\n'
               f'- Scheduled sessions: **{metrics.get("scheduled_sessions", "not reported")}**\n'
               f'- Detected hard violations: **{metrics.get("hard_violations", "not reported")}**\n')
    if errors:
        message += '\n**Detected issues:**\n' + '\n'.join('- ' + norm(x.get('message')) for x in errors[:15])
        if len(errors)>15:
            message += f'\n\n{len(errors)-15} additional issues are available in Validation & log.'
    else:
        message += '\nNo hard violations were reported by the implemented checks.'
    message += ('\n\n**Limit:** Synthetic assignments and absent individual-student enrolment records '
                'prevent full real-world verification.')
    return message


def _named_teachers_in_question(question, teacher_names):
    """Retrieve all explicitly named teachers, not just the first match."""
    names = {name for name in teacher_names if match_value(question, (name,))}
    return sorted(names)


def _common_free_periods(teachers, day, rows, data, faculty):
    """Intersect two teachers' *confirmed* free periods, not just vacant rows."""
    slots = sorted((s for s in data.get('Time_Slots', [])
                    if norm(s.get('day')).upper() == day),
                   key=lambda s: int_value(s.get('period')))
    common = []
    unconfirmed = []
    for slot in slots:
        p = int_value(slot.get('period'))
        checks = [_teacher_slot_status(t, day, p, rows, data, faculty)[1]
                  for t in teachers]
        if all(state == 'AVAILABLE' for state in checks):
            start = _slot_time_minutes(norm(slot.get('start_time')))
            end = _slot_time_minutes(norm(slot.get('end_time')))
            label = (f' ({_clock_label(start)}–{_clock_label(end)})'
                     if start is not None and end is not None else '')
            common.append(f'Period {p}{label}')
        elif 'UNKNOWN' in checks and not any(s in ('BUSY', 'BLOCKED') for s in checks):
            unconfirmed.append(str(p))
    together = ' and '.join(teachers)
    when = DAY_NAMES.get(day, day)
    if common:
        result = f'**{together} are both free on {when} during:**\n\n' + '\n'.join('- ' + x for x in common)
    else:
        result = f'**No confirmed common free period** for {together} on {when}.'
    if unconfirmed:
        result += '\n\nSome periods cannot be confirmed from the faculty availability records.'
    return result


def _free_rooms_by_period(day, rows, data, rooms, *, kind='All', seats=0,
                          show_all=False):
    """Grounded whole-day room search, rather than demanding a period.

    A room is not intrinsically 'free on Tuesday': availability changes every
    period. Show a complete period-by-period list by default, with a concise
    listing only on explicit request. These are room allocations only, not a claim
    about equipment, faculty or group feasibility.
    """
    slots = sorted((slot for slot in data.get('Time_Slots', [])
                    if norm(slot.get('day')).upper() == day),
                   key=lambda slot: int_value(slot.get('period')))
    eligible = {rid for rid, rec in rooms.items()
                if (kind == 'All' or norm(rec.get('room_type')).lower() == kind.lower())
                and int_value(rec.get('capacity')) >= seats}
    used = {(int_value(r.get('period')), norm(r.get('room_id')))
            for r in rows if norm(r.get('day')).upper() == day}
    label = DAY_NAMES.get(day, day)
    if not slots:
        return f'No teaching periods are defined for {label}.'
    if not eligible:
        return f'No {kind.lower()} rooms match the requested capacity in the uploaded dataset.'
    lines = []
    ever_free = set()
    free_all_day = set(eligible)
    for slot in slots:
        period = int_value(slot.get('period'))
        available = sorted(rid for rid in eligible if (period, rid) not in used)
        ever_free.update(available)
        free_all_day.intersection_update(available)
        start = _slot_time_minutes(norm(slot.get('start_time')))
        end = _slot_time_minutes(norm(slot.get('end_time')))
        time = (f' ({_clock_label(start)}–{_clock_label(end)})'
                if start is not None and end is not None else '')
        shown = available if show_all else available[:5]
        names = ', '.join(shown) if shown else 'none'
        extra = (f', +{len(available)-len(shown)} more'
                 if len(available) > len(shown) else '')
        lines.append(f'- **Period {period}{time}: {len(available)} free** — {names}{extra}')
    group_label = 'rooms' if kind == 'All' else kind.lower() + 's'
    capacity_note = f' with at least {seats} seats' if seats else ''
    all_day_summary = (f' **{len(free_all_day)}** remain unallocated all day'
                       + (': ' + ', '.join(sorted(free_all_day) if show_all else sorted(free_all_day)[:5])
                          + (f', +{len(free_all_day)-5} more' if not show_all and len(free_all_day)>5 else '')
                          + '.' if free_all_day else '')
                       if free_all_day else ' No room remains unallocated all day.')
    return (f'**Available {group_label} on {label}{capacity_note}, by period:**\n\n'
            + '\n'.join(lines) + '\n\n'
            + f'{len(ever_free)} distinct {group_label} have at least one free period.'
            + all_day_summary
            + ('\n\nAsk for a specific period to see every available room.'
               if not show_all else '')
            + '\n\nRoom-only availability; a class move still requires independent validation.')


def _free_rooms_for_consecutive_periods(day, rows, data, rooms, *, kind='Lab', seats=0,
                                        start_period=None):
    """Find every contiguous two-period room window, across a requested day.

    Only claims unallocated room windows; full placement needs independent
    checks for faculty, group, equipment, and institution-specific policies.
    """
    slots = sorted((s for s in data.get('Time_Slots', [])
                    if norm(s.get('day')).upper() == day),
                   key=lambda s: int_value(s.get('period')))
    candidates = {rid for rid, rec in rooms.items()
                  if norm(rec.get('room_type')).lower() == kind.lower()
                  and int_value(rec.get('capacity')) >= seats}
    occupied = {(int_value(r.get('period')), norm(r.get('room_id')))
                for r in rows if norm(r.get('day')).upper() == day}
    windows = []
    for a, b in zip(slots, slots[1:]):
        p, q = int_value(a.get('period')), int_value(b.get('period'))
        if q != p + 1 or (start_period is not None and p != start_period):
            continue
        free = sorted(rid for rid in candidates
                      if (p, rid) not in occupied and (q, rid) not in occupied)
        if not free:
            continue
        start = _slot_time_minutes(norm(a.get('start_time')))
        end = _slot_time_minutes(norm(b.get('end_time')))
        time = f' ({_clock_label(start)}–{_clock_label(end)})' if start is not None and end is not None else ''
        windows.append((p, q, time, free))
    label = DAY_NAMES.get(day, day)
    if not windows:
        return f'No unallocated {kind.lower()} is confirmed for two consecutive periods on {label}.'
    cap = f' with at least {seats} seats' if seats else ''
    if start_period is not None:
        p, q, time, free = windows[0]
        return (f'**{len(free)} {kind.lower()}(s) are unallocated on {label}, '
                f'Periods {p}–{q}{time}{cap}:**\n\n' + ', '.join(free)
                + '\n\nRoom-only check; the complete class move still requires validation.')
    distinct = sorted({rid for _, _, _, free in windows for rid in free})
    counts = ', '.join(f'P{p}–{q}: {len(free)}' for p, q, _, free in windows)
    return (f'**{len(distinct)} {kind.lower()}(s) have at least one two-period opening '
            f'on {label}{cap}:**\n\n' + ', '.join(distinct)
            + f'\n\n**Available windows (room counts):** {counts}. '
              'Ask for a starting period to see the exact rooms for that window. '
              'These are room-only openings; a complete class move still requires validation.')


def _room_capacity_requested(question):
    m = re.search(r'\b(?:at least|minimum|min\.?|capacity of|for)\s*(\d{1,3})\s*(?:students?|seats?)\b', question, flags=re.IGNORECASE)
    return int(m.group(1)) if m else 0


def response(question, rows, data, validation_report=None):
    """Answer supported coordinator questions strictly from schedule and workbook.

    The fallback explicitly explains unsupported requests; no invented solutions.
    """
    q = norm(question)
    if not q:
        return 'Ask about a section, faculty member, course, room, or validation result.'
    low = q.lower()
    groups = {norm(r.get('group_id')): r for r in data.get('Student_Groups', [])}
    rooms = {norm(r.get('room_id')): r for r in data.get('Rooms', [])}
    courses = {norm(r.get('course_id')): r for r in data.get('Courses', [])}
    faculty = {norm(r.get('faculty_id')): r for r in data.get('Faculty', [])}
    teacher_names = {norm(r.get('faculty_name')) for r in rows if norm(r.get('faculty_name'))}
    teacher_names.update(norm(r.get('faculty_name')) for r in faculty.values() if norm(r.get('faculty_name')))
    teacher, teacher_fuzzy = match_teacher(q, teacher_names)
    group = _group_match(q, groups, rows)
    course, ambiguous_course_ids = match_course_reference(q, courses, rows)
    if ambiguous_course_ids:
        names = ', '.join(f'{code} ({norm(courses.get(code, {}).get("course_name"))})'
                          for code in ambiguous_course_ids)
        return f'That subject name matches multiple courses: **{names}**. Which course code do you mean?'
    room = match_value(q, rooms)
    day = parse_day(q)
    period = parse_period(q)
    day_label = DAY_NAMES.get(day, '')
    clock_minutes = parse_clock_minutes(q)
    # For hour-only 12-hour clock text like "2:30" without AM/PM, infer PM
    # only if the AM interpretation is outside teaching slots and the PM
    # interpretation is uniquely inside. Never change an explicitly typed AM.
    if clock_minutes is not None and day and clock_minutes < 8 * 60 and re.search(
            r'(?<!\w)\d{1,2}[:.]\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?', q, re.IGNORECASE):
        has_meridiem = bool(re.search(
            r'(?<!\w)\d{1,2}[:.]\d{2}\s*(?:a\.?m\.?|p\.?m\.?)', q, re.IGNORECASE))
        if not has_meridiem and _slot_for_clock_time(data, day, clock_minutes) is None \
                and _slot_for_clock_time(data, day, clock_minutes + 12 * 60) is not None:
            clock_minutes += 12 * 60
    availability_intent = bool(re.search(r'\b(free|available|availability|vacant|empty|occupied|busy)\b', low)
                               or re.search(r'\b(can i meet|when can i meet|have time to meet|available to meet)\b', low))
    # Requests to book a new teaching window are not lookups of an existing
    # class. Check the whole requested duration before filtering rows.
    practical_intent = bool(re.search(r'\b(two[- ]hour|2[- ]hour|two consecutive periods|2 consecutive periods)\b', low)
                            and re.search(r'\b(practical|lab|class|teach|session)\b', low))
    if teacher and day and practical_intent and len(_mentioned_clock_times(q)) >= 1:
        return _two_period_teacher_check(q, teacher, day, rows, data, faculty)
    common_requested = bool(re.search(r'\b(common|mutual|both|together|overlapping)\b', low)
                            and re.search(r'\b(free|available|availability|meet|slot|period)\b', low))
    if common_requested:
        named = _named_teachers_in_question(q, teacher_names)
        if len(named) == 2:
            if not day:
                return 'Which weekday should I check for a common free period?'
            return _common_free_periods(named, day, rows, data, faculty)
        if len(named) > 2:
            return 'Please specify exactly two teachers for a common availability check.'
    if course and (group or day) and re.search(r'\b(can|could|may|would|possible)\b.*\b(schedule|place|hold)\b', low):
        return _proposed_class_answer(q, course, group, data, rows)
    ordinal = None
    if teacher and day and period is None and clock_minutes is None:
        ordinal = _ordinal_period(q)
        if ordinal is not None:
            period = ordinal
            availability_intent = True
    if teacher and day and (re.search(r'\bnot teaching\b', low) or
                            re.search(r'\bwhen\b.*\b(free|available)\b', low)) and period is None and clock_minutes is None:
        return _teacher_free_periods(teacher, day, rows, data, faculty)
    # A sentence can mention two separate weekdays/times. Resolve every one;
    # a single parse_day() would otherwise silently discard the second.
    day_segments = _mentioned_day_segments(q)
    if teacher and availability_intent and len(day_segments) > 1:
        result = _teacher_multiple_slot_status(q, teacher, day_segments, rows, data, faculty)
        return result
    if clock_minutes is not None and day:
        actual_slot = _slot_for_clock_time(data, day, clock_minutes)
        if actual_slot is None:
            return (f'**{_clock_label(clock_minutes)} on {day_label}** does not fall inside '
                    'a teaching period defined in your uploaded Excel Time_Slots. '
                    'I cannot verify class availability at that clock time.')
        resolved_period = int_value(actual_slot.get('period'))
        if period is not None and period != resolved_period:
            return (f'Your question mentions **Period {period}**, but '
                    f'**{_clock_label(clock_minutes)} on {day_label}** falls in '
                    f'**Period {resolved_period}** in the uploaded Excel workbook. '
                    'Please clarify which time you want checked.')
        period = resolved_period
    elif clock_minutes is not None and day is None:
        return 'Please include a weekday with the clock time so I can check the correct Excel slot.' 

    if teacher and day and period is not None and re.search(
            r'\b(synthetic|real[- ]world|source|provenance|verified|official)\b', low):
        return _teacher_availability_provenance(teacher, day, period, data, faculty)

    # Read-only safeguards take precedence over schedule lookup, even if a named course appears.
    if any(token in low for token in ('reschedule', 'move ', 'shift ', 'swap ', 'cancel ',
                                       'change schedule', 'modify timetable', 'replace teacher')):
        return ('I can check the present schedule, but this version **does not change** the timetable. '
                'A change must be proposed through CP-SAT, validated and approved by the coordinator. '
                'Nothing has been modified.')
    if any(token in low for token in ('why infeasible', 'infeasible', 'why not scheduled',
                                       'reason for failure', 'why could not')):
        return ('A specific failure explanation requires the CP-SAT solver diagnostic output. '
                'I cannot infer a valid reason from a completed timetable. '
                'Open **Results → Validation & log** for the relevant run.')
    if any(token in low for token in ('validation', 'violation', 'clash', 'conflict',
                                      'timetable valid', 'timetable correct', 'verified schedule')):
        return _validation_answer(validation_report)
    if any(t in low for t in ('soft constraint', 'soft preference', 'penalty', 'avoid thursday', 'thursday afternoon')):
        policies = load_policies(data)
        slots = {(norm(s.get('day')), int_value(s.get('period'))):norm(s.get('slot_id')) for s in data.get('Time_Slots', [])}
        used = [r for r in rows if slots.get((norm(r.get('day')), int_value(r.get('period')))) in policies.avoid_slot_ids]
        return (f'**Thursday afternoon is a soft-avoid preference, not a ban.** '
                f'The scheduler tries to keep {", ".join(sorted(policies.avoid_slot_ids))} free '
                f'(normally Thursday Periods 7–8, 2:30–4:20 PM). '
                f'Classes can still be placed there if needed. '
                f'The selected timetable has **{len(used)} occupied session-period rows** '
                f'in these slots. The preference weight is **{policies.avoid_weight}** per row.')

    occupied = [r for r in rows if (day is None or norm(r.get('day')) == day)
                and (period is None or int_value(r.get('period')) == period)]
    # Intent is evaluated before parsing the weekday/time combination.
    room_intent = bool(room or re.search(r'\b(?:classrooms?|rooms?|laborator(?:y|ies)|labs?)\b', low))
    if availability_intent and room_intent:
        two_period_room_query = bool(re.search(
            r'\b(?:two|2)\s*(?:continuous|consecutive|back[- ]to[- ]back|hour|period)', low)
            or re.search(r'\b(?:continuous|consecutive)\s+(?:two|2)\b', low))
        if day and two_period_room_query and room is None:
            return _free_rooms_for_consecutive_periods(
                day, rows, data, rooms,
                kind='Lab' if re.search(r'\b(?:labs?|laborator(?:y|ies))\b', low) else 'Classroom',
                seats=_room_capacity_requested(q), start_period=period)
        if day is None or period is None:
            return 'Specify a **weekday and period**, for example: “Which classrooms are free on Monday Period 3?”'
        used_ids = {norm(r.get('room_id')) for r in occupied}
        if room:
            matching = [r for r in occupied if norm(r.get('room_id')) == room]
            if matching:
                entries = _merge_sessions(_whole_blocks(rows, matching))
                return (f'**{room} is allocated on {day_label}, Period {period}.**\n\n' +
                        '\n'.join('- ' + _format_session(r) for r in entries))
            return (f'**{room} is not allocated** on {day_label}, Period {period} in the generated timetable. '
                    'This does not guarantee availability outside the uploaded scheduling data.')
        kind = 'Lab' if re.search(r'\b(?:labs?|laborator(?:y|ies))\b', low) else 'Classroom'
        capacity_match = re.search(r'\b(?:for|capacity|at least)\s*(\d{1,3})\s*(?:students?|seats?)\b', low)
        min_capacity = int(capacity_match.group(1)) if capacity_match else 0
        suitable = [rid for rid, r in rooms.items()
                    if (kind == 'All' or norm(r.get('room_type')).lower() == kind.lower())
                    and int_value(r.get('capacity')) >= min_capacity and rid not in used_ids]
        return (f'**{len(suitable)} {kind.lower()}(s) not allocated** on {day_label}, Period {period}'
                + (f' with at least {min_capacity} seats' if min_capacity else '')
                + ':\n\n' + _format_room_list(suitable, rooms) + '\n\n'
                'Availability is checked only against this generated timetable and uploaded room inventory.')

    if teacher and availability_intent and day and period:
        note = ''
        checked, state = _teacher_slot_status(teacher, day, period, rows, data, faculty,
                                              clock_minutes=clock_minutes)
        # "Is she occupied?" asks the reverse of "Is she free?". Never say
        # "No" to a busy/occupied question and then claim she is teaching.
        if re.search(r'\b(occupied|busy)\b', low):
            if state == 'BUSY':
                checked = checked.replace(f'**No — {teacher} is teaching**',
                                          f'**Yes — {teacher} is occupied (teaching)**', 1)
            elif state == 'AVAILABLE':
                checked = checked.replace(f'**Yes — {teacher} is free**',
                                          f'**No — {teacher} is not occupied**', 1)
            elif state == 'BLOCKED':
                checked = (f'**No teaching session is assigned to {teacher}** in this period, '
                           'but the timetable marks it unavailable for scheduling.')
        if ordinal is not None and 'class' in low:
            note = ('_Interpreting "' + next((k for k, v in _ORDINAL_PERIODS.items()
                         if v == ordinal and k.isalpha()), str(ordinal))
                    + ' class" as Period ' + str(ordinal) + '._\n\n') + note
        return note + checked

    if teacher and availability_intent and day and period is None and clock_minutes is None:
        return _teacher_day_status(teacher, day, rows, data, faculty)
    if teacher and availability_intent and day is None:
        return 'Which weekday should I check for **' + teacher + '**?'

    # A teacher-and-section question needs an allocation directory, not just
    # a list of names and Theory/Practical labels.
    teaching_intent = bool(re.search(r'\b(who teaches|who teach|teachers?|faculty|professors?|instructors?|teaching staff)\b', low))
    section_intent = bool(re.search(r'\b(sections?|batches?|groups?)\b', low)
                          and re.search(r'\b(teach|teaches|teaching|teachers?|faculty|instructors?|who|which|list|show)\b', low))
    if course and (teaching_intent or section_intent) and not day and not period:
        matching = [r for r in rows if norm(r.get('course_id')) == course
                    and (not teacher or norm(r.get('faculty_name')) == teacher)
                    and (not group or _belongs(r, group, groups))]
        details = _course_teaching_assignments(course, matching, groups,
                                               include_sections=section_intent)
        if not details:
            return f'No scheduled teaching allocation was found for **{course}** with the requested filters.'
        names = {norm(r.get('faculty_name')) for r in matching if norm(r.get('faculty_name'))}
        title = f'**{course} · {norm(courses.get(course, {}).get("course_name"))}**'
        if group:
            title += f' for **{group}**'
        if teacher:
            title += f' — **{teacher}**'
        heading = (f'**{len(names)} scheduled faculty member(s) and their sections:**'
                   if section_intent else f'{len(names)} scheduled faculty member(s):')
        return title + f'\n\n{heading}\n\n' + details

    # Direct faculty->sections lookup. Do not expand into an eight-session
    # timetable unless the user explicitly requests a schedule or days/times.
    teacher_section_intent = bool(
        (re.search(r'\b(sections?|student groups?|batches?)\b', low)
         and re.search(r'\b(which|what|all|list|show|teach|teaches|teaching|handle|handles|assigned)\b', low))
        or (re.search(r'\b(which|what|all)\b.*\b(classes?|groups?)\b.*\b(teach|teaches|handle|handles)\b', low)
            and day is None)
    )
    if teacher and not course and teacher_section_intent and period is None and clock_minutes is None:
        include_courses = bool(re.search(r'\b(courses?|subjects?)\b', low))
        include_batches = bool(re.search(r'\b(batches?|labs?|practicals?)\b', low))
        return _teacher_section_directory(teacher, rows, groups, day=day,
                                          include_courses=include_courses,
                                          include_batches=include_batches)

    # Course-directory questions for an original section.
    if group and (re.search(r'\b(subjects?|courses?|teaching team|faculty list|subject list|course list)\b', low)
                  or (teaching_intent and not course and not teacher)) and not course and not day:
        matching = [r for r in rows if _belongs(r, group, groups)]
        by_course = defaultdict(lambda: {'title':'', 'people':set()})
        for r in matching:
            item = by_course[norm(r.get('course_id'))]
            item['title'] = norm(r.get('course_name'))
            item['people'].add(norm(r.get('faculty_name')))
        if not by_course:
            return f'No scheduled courses were found for **{group}**.'
        details = '\n'.join(f'- **{code} · {info["title"]}** — '+', '.join(sorted(info['people']))
                            for code, info in sorted(by_course.items()))
        return f'**{group} — {len(by_course)} scheduled course(s)**\n\n' + details

    if teacher and re.search(r'\b(which|what|list|show)\b.*\b(courses?|subjects?)\b', low):
        subjects = defaultdict(set)
        for r in rows:
            if norm(r.get('faculty_name')) == teacher and (not day or norm(r.get('day')) == day):
                subjects[(norm(r.get('course_id')), norm(r.get('course_name')))].add(norm(r.get('group_id')))
        if not subjects:
            return f'No courses found for **{teacher}** with those filters.'
        detail = '\n'.join(f'- **{c} · {title}** — groups: '+', '.join(sorted(group_ids))
                           for (c, title), group_ids in sorted(subjects.items()))
        return f'**{teacher} teaches {len(subjects)} course(s)**\n\n' + detail

    if re.search(r'\b(how many|number of|count)\b', low) and (teacher or group or course):
        selected = [r for r in rows
                    if (not teacher or norm(r.get('faculty_name')) == teacher)
                    and (not group or _belongs(r, group, groups))
                    and (not course or norm(r.get('course_id')) == course)
                    and (not day or norm(r.get('day')) == day)]
        title = ' · '.join(x for x in (teacher, group, course, day_label) if x)
        return (f'**{title}** has **{len(_merge_sessions(selected))} scheduled session(s)** '
                f'covering **{len(selected)} occupied session-period rows** in the generated timetable. '
                'Two-period practicals count as one session.')

    if teacher or group or course or (room and not availability_intent):
        filtered = [r for r in rows
                    if (not teacher or norm(r.get('faculty_name')) == teacher)
                    and (not group or _belongs(r, group, groups))
                    and (not course or norm(r.get('course_id')) == course)
                    and (not room or norm(r.get('room_id')) == room)
                    and (not day or norm(r.get('day')) == day)]
        if period is not None:
            matching_period = [r for r in filtered if int_value(r.get('period')) == period]
            filtered = _whole_blocks(filtered, matching_period)
        parts = [x for x in (teacher, group, course, room, day_label, f'Period {period}' if period else None) if x]
        return _display_matches(filtered, ' · '.join(parts))

    if any(t in low for t in ('summary', 'overview', 'statistics', 'stats', 'how many sessions', 'how many classes')):
        return _count_summary(rows)

    return ('I could not identify a supported timetable query from that wording. '
            'Try including a **teacher name, section ID, course code, room ID, day or period**.\n\n'
            '**Examples:**\n'
            '- Show the complete MCA-III-A timetable on Tuesday\n'
            '- Which courses and teachers does BTECHCSE-V-AB have?\n'
            '- Who teaches CSL315?\n'
            '- Is Aarti Kukreja free on Tuesday Period 3?\n'
            '- Which labs with at least 30 seats are free on Friday Period 4?\n'
            '- Show validation issues\n\n'
            'Cloud AI interpretation is optional. Timetable changes require coordinator approval.')
