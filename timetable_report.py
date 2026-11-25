"""Presentation-only HTML timetable report.

No changes to CP-SAT constraints, slot selection, or room allocation.
This module turns the solver's already-validated output rows into a polished
standalone HTML timetable with functioning section navigation and course lists.
"""
from __future__ import annotations

import html
import re
from collections import defaultdict

DAY_NAMES = {'MON': 'Monday', 'TUE': 'Tuesday', 'WED': 'Wednesday',
             'THU': 'Thursday', 'FRI': 'Friday', 'SAT': 'Saturday', 'SUN': 'Sunday'}


def _text(value):
    return html.escape(str(value if value is not None else ''), quote=True)


def _kind(kind):
    k = str(kind or '').upper()
    if k == 'PRACTICAL':
        return 'Practical', 'practical'
    if k == 'TUTORIAL':
        return 'Tutorial', 'tutorial'
    if 'MENTOR' in k:
        return 'Mentoring', 'mentoring'
    return 'Theory', 'theory'


def _original_memberships(groups):
    memberships = defaultdict(set)
    for gid, record in groups.items():
        kind = str(record.get('group_kind', '')).strip()
        if kind == 'PARENT':
            memberships[gid].add(gid)
        elif kind == 'LAB_BATCH':
            parent = str(record.get('parent_group_id', '')).strip()
            if parent:
                memberships[gid].add(parent)
        else:
            for member in str(record.get('synthetic_membership_by_original_section', '')).split(';'):
                parent = member.split('=')[0].strip()
                if parent in groups:
                    kind = str(groups[parent].get('group_kind', '')).strip()
                    if kind == 'PARENT': memberships[gid].add(parent)
                    elif kind == 'LAB_BATCH':
                        original = str(groups[parent].get('parent_group_id', '')).strip()
                        if original in groups: memberships[gid].add(original)
    return memberships


def _section_rows(rows, groups, sections):
    memberships = _original_memberships(groups)
    result = {sec: [] for sec in sections}
    for r in rows:
        gid = str(r.get('group_id', ''))
        course = str(r.get('course_id', ''))
        parent = str(groups.get(gid, {}).get('parent_group_id', '')).strip()
        targets = set()
        if gid in result:
            targets.add(gid)
        if parent in result:
            targets.add(parent)
        if str(groups.get(gid,{}).get('group_kind','')).startswith('MIXED_') or (
                str(groups.get(gid,{}).get('group_kind','')) in ('SHARED_LANGUAGE_THEORY','SHARED_VALUE_ADDITION_COHORT')):
            targets.update(memberships.get(gid, set()) & result.keys())
        for section in targets:
            result[section].append(r)
    return result


def _course_summary(rows):
    by_course = {}
    for row in rows:
        code = str(row.get('course_id', '')).strip()
        if not code:
            continue
        item = by_course.setdefault(code, {
            'title': str(row.get('course_name', '')).strip() or 'Course name unavailable',
            'faculty': defaultdict(set), 'types': set(), 'groups': set()
        })
        title = str(row.get('course_name', '')).strip()
        if title:
            item['title'] = title
        faculty = str(row.get('faculty_name', '')).strip()
        name, _ = _kind(row.get('kind'))
        if faculty:
            item['faculty'][faculty].add(name)
        item['types'].add(name)
        if row.get('group_id'):
            item['groups'].add(str(row['group_id']))
    return by_course


def _course_table(rows):
    courses = _course_summary(rows)
    out = ['<div class="course-heading"><div>',
           '<div class="section-eyebrow">SECTION DIRECTORY</div>',
           '<h3>Courses &amp; teaching faculty</h3>',
           '<p>Course codes, full names and assigned teaching staff for this section.</p>',
           '</div><span class="count-chip">' + str(len(courses)) + ' courses</span></div>']
    if not courses:
        out.append('<p class="empty-note">No scheduled courses are available for this section.</p>')
        return ''.join(out)
    out.append('<div class="course-table-wrap"><table class="course-table"><thead>'
               '<tr><th>Course code</th><th>Course name</th><th>Faculty / teaching staff</th>'
               '<th>Class type</th></tr></thead><tbody>')
    for code, details in sorted(courses.items()):
        teachers = []
        for teacher, types in sorted(details['faculty'].items()):
            teacher_types = ', '.join(sorted(types))
            teachers.append('<span class="faculty-line"><span class="faculty-dot"></span>'
                            f'<span><strong>{_text(teacher)}</strong>'
                            f'<small>{_text(teacher_types)}</small></span></span>')
        if not teachers:
            teachers = ['<span class="muted">Not assigned</span>']
        kinds = ''.join(f'<span class="type-mini">{_text(t)}</span>' for t in sorted(details['types']))
        out.append('<tr>'
                   f'<td><span class="course-code">{_text(code)}</span></td>'
                   f'<td class="course-title">{_text(details["title"])}</td>'
                   f'<td>{"".join(teachers)}</td>'
                   f'<td><div class="type-mini-row">{kinds}</div></td>'
                   '</tr>')
    out.append('</tbody></table></div>')
    return ''.join(out)


def _faculty_course_table(rows):
    """Course/section directory for a single faculty member, from scheduled rows."""
    directory = {}
    for row in rows:
        code = str(row.get('course_id', '')).strip()
        if not code:
            continue
        item = directory.setdefault(code, {'name': '', 'groups': set(), 'types': set()})
        item['name'] = str(row.get('course_name') or item['name'] or '').strip()
        group = str(row.get('group_id', '')).strip()
        if group:
            item['groups'].add(group)
        kind, _ = _kind(row.get('kind'))
        item['types'].add(kind)
    out = ['<div class="course-heading"><div><div class="section-eyebrow">FACULTY DIRECTORY</div>',
           '<h3>Courses &amp; assigned groups</h3>',
           '<p>Courses, section or batch assignments and session types for this faculty member.</p>',
           '</div><span class="count-chip">' + str(len(directory)) + ' courses</span></div>']
    if not directory:
        return ''.join(out) + '<p class="empty-note">No scheduled courses.</p>'
    out.append('<div class="course-table-wrap"><table class="course-table"><thead><tr>'
               '<th>Course code</th><th>Course name</th><th>Sections / groups</th><th>Class type</th>'
               '</tr></thead><tbody>')
    for code, item in sorted(directory.items()):
        groups = ', '.join(sorted(item['groups']))
        types = ''.join(f'<span class="type-mini">{_text(t)}</span>' for t in sorted(item['types']))
        out.append(f'<tr><td><span class="course-code">{_text(code)}</span></td>'
                   f'<td class="course-title">{_text(item["name"] or "Course name unavailable")}</td>'
                   f'<td>{_text(groups)}</td><td><div class="type-mini-row">{types}</div></td></tr>')
    out.append('</tbody></table></div>')
    return ''.join(out)


def _entry_card(row, highlighted_session_ids=frozenset()):
    name, kind_class = _kind(row.get('kind'))
    code = _text(row.get('course_id', ''))
    title = _text(row.get('course_name', ''))
    faculty = _text(row.get('faculty_name', ''))
    room = str(row.get('room_id', '')).strip()
    lab_label = 'Synthetic room' if room.startswith('SYNTHETIC_LAB') else room
    # Provenance remains in timetable_all.csv and the validation report.
    # Repeated per-session badges are deliberately omitted from the timetable UI.
    group = str(row.get('group_id', '')).strip()
    # Group is useful for parallel lab batches and language electives.
    group_label = (f'<span class="entry-group" title="Assigned group">{_text(group)}</span>'
                   if group else '')
    moved = str(row.get('session_id', '')).strip() in highlighted_session_ids
    flag = '<span class="rescheduled-flag">MOVED</span>' if moved else ''
    marker = ' session-rescheduled' if moved else ''
    return (f'<div class="session session-{kind_class}{marker}" title="{title}">'
            f'{flag}'
            f'<div class="session-heading"><strong>{code}</strong><span class="session-type">{_text(name)}</span></div>'
            f'<div class="session-title">{title}</div>'
            f'<div class="session-teacher">{faculty}</div>'
            f'<div class="session-meta"><span>{_text(lab_label)}</span></div>'
            f'{group_label}</div>')


def _faculty_panel(faculty, rows, days, display_rows, time_by_period, highlighted_session_ids, index,
                   university_name=None, mentor_sections=()):
    """Render a faculty week from the same verified CSV session rows as section view."""
    by_slot = defaultdict(list)
    for row in rows:
        try:
            period = int(float(row.get('period', 0)))
        except (TypeError, ValueError):
            continue
        by_slot[(str(row.get('day', '')).strip().upper(), period)].append(row)
    faculty_id = f'faculty-{index:03d}'
    course_count = len({str(r.get('course_id', '')).strip() for r in rows if r.get('course_id')})
    section_count = len({str(r.get('group_id', '')).strip() for r in rows if r.get('group_id')})
    session_count = len({str(r.get('session_id', '')).strip() for r in rows if r.get('session_id')})
    occupied_periods = len({(str(r.get('day', '')).strip().upper(), int(float(r.get('period', 0))))
                            for r in rows if str(r.get('period','')).strip()})
    badges = ''.join(f'<span>{value} {label}</span>' for value, label in (
        (course_count, 'courses'), (section_count, 'groups'),
        (session_count, 'weekly sessions'), (occupied_periods, 'occupied periods')))
    out = [f'<section class="section-card timetable-panel" id="{faculty_id}" data-view="faculty" '
           f'data-faculty="{_text(faculty)}" hidden>',
           _university_heading(university_name),
           f'<div class="section-title"><div><div class="section-eyebrow">FACULTY-WISE TIMETABLE</div>'
           f'</div><div class="section-badges">{badges}</div></div>',
           f'<div class="timetable-selected-name" aria-label="Selected faculty"><h2>{_text(faculty)}</h2></div>',
           '<div class="grid-scroll"><table class="timetable"><thead><tr><th>Time slot</th>']
    out.extend(f'<th>{DAY_NAMES.get(day, day.title())}</th>' for day in days)
    out.append('</tr></thead><tbody>')
    for row_type, payload in display_rows:
        if row_type == 'block':
            out.append(_fixed_block_row(payload, days, mentor_sections=mentor_sections, faculty_view=True))
            continue
        period = payload
        start, end = time_by_period.get(period, ('', ''))
        label_time = f'{_text(start)}–{_text(end)}' if start or end else ''
        out.append('<tr><td><span class="period-name">Period ' + str(period) + '</span>'
                   f'<span class="period-time">{label_time}</span></td>')
        for day in days:
            entries = sorted(by_slot.get((day, period), []),
                             key=lambda r:(str(r.get('course_id','')), str(r.get('group_id',''))))
            cells = ''.join(_entry_card(r, highlighted_session_ids) for r in entries)
            out.append(f'<td>{cells}</td>' if cells else '<td><span class="day-empty">–</span></td>')
        out.append('</tr>')
    out.append('</tbody></table></div><div class="courses-block">')
    out.append(_faculty_course_table(rows))
    out.append('</div></section>')
    return '\n'.join(out)


def _room_course_table(rows):
    """Room occupancy directory: course, teaching group and actual faculty."""
    directory = {}
    for row in rows:
        code = str(row.get('course_id') or '').strip()
        if not code:
            continue
        item = directory.setdefault(code, {'name':'', 'groups':set(), 'faculty':set()})
        item['name'] = str(row.get('course_name') or item['name'] or '').strip()
        if row.get('group_id'):
            item['groups'].add(str(row['group_id']))
        if row.get('faculty_name'):
            item['faculty'].add(str(row['faculty_name']))
    if not directory:
        return '<p class="empty-note">No teaching sessions assigned to this room in the selected timetable.</p>'
    out = ['<div class="course-table-wrap"><table class="course-table"><thead><tr>'
           '<th>Course</th><th>Subject</th><th>Sections / groups</th><th>Faculty</th>'
           '</tr></thead><tbody>']
    for code,item in sorted(directory.items()):
        out.append(f'<tr><td><span class="course-code">{_text(code)}</span></td>'
                   f'<td class="course-title">{_text(item["name"])}</td>'
                   f'<td>{_text(", ".join(sorted(item["groups"])))}</td>'
                   f'<td>{_text(", ".join(sorted(item["faculty"])))}</td></tr>')
    out.append('</tbody></table></div>')
    return ''.join(out)


def _room_panel(room, rows, days, display_rows, time_by_period, highlighted_session_ids, index,
                university_name=None, room_info=None):
    """Display verified occupancy of one room; empty configured rooms remain visible."""
    by_slot = defaultdict(list)
    for row in rows:
        try:
            period = int(float(row.get('period', 0)))
        except (TypeError, ValueError):
            continue
        by_slot[(str(row.get('day', '')).strip().upper(), period)].append(row)
    room_id = f'room-{index:03d}'
    details = room_info or {}
    description = str(details.get('room_type') or 'Room').strip()
    capacity = str(details.get('capacity') or '').strip()
    badges = [f'<span>{_text(description)}</span>']
    if capacity:
        badges.append(f'<span>Capacity {_text(capacity)}</span>')
    session_count = len({r.get('session_id') for r in rows if r.get('session_id')})
    badges.append(f'<span>{session_count} weekly session{"s" if session_count != 1 else ""}</span>')
    out = [f'<section class="section-card timetable-panel" id="{room_id}" data-view="room" '
           f'data-room="{_text(room)}" hidden>',
           _university_heading(university_name),
           '<div class="section-title"><div><div class="section-eyebrow">ROOM-WISE TIMETABLE</div>'
           '</div><div class="section-badges">' + ''.join(badges) + '</div></div>',
           f'<div class="timetable-selected-name" aria-label="Selected room"><h2>{_text(room)}</h2></div>',
           '<div class="grid-scroll"><table class="timetable"><thead><tr><th>Time slot</th>']
    out.extend(f'<th>{DAY_NAMES.get(day, day.title())}</th>' for day in days)
    out.append('</tr></thead><tbody>')
    for row_type, payload in display_rows:
        if row_type == 'block':
            out.append(_fixed_block_row(payload, days, room_view=True))
            continue
        period = payload
        start, end = time_by_period.get(period, ('', ''))
        out.append('<tr><td><span class="period-name">Period ' + str(period) + '</span>'
                   f'<span class="period-time">{_text(start)}–{_text(end)}</span></td>')
        for day in days:
            entries = sorted(by_slot.get((day, period), []),
                             key=lambda r:(str(r.get('course_id','')), str(r.get('group_id',''))))
            cells = ''.join(_entry_card(r, highlighted_session_ids) for r in entries)
            out.append(f'<td>{cells}</td>' if cells else '<td><span class="day-empty">–</span></td>')
        out.append('</tr>')
    out.append('</tbody></table></div>')
    out.append('<div class="courses-block"><div class="course-heading"><div>'
               '<div class="section-eyebrow">ROOM OCCUPANCY</div><h3>Scheduled teaching in this room</h3>'
               '<p>Only verified placements are shown; an empty period does not by itself prove a class move is feasible.</p>'
               '</div></div>')
    out.append(_room_course_table(rows))
    out.append('</div></section>')
    return '\n'.join(out)


def _university_heading(university_name):
    """Show the selected university immediately above each timetable grid."""
    name = str(university_name or '').strip()
    if not name or name.casefold() == 'not specified':
        return ''
    return ('<div class="timetable-university-name" aria-label="University">'
            + _text(name) + '</div>')


def _mentor_names(group):
    """Read the exact original mentor labels from the workbook's group mapping."""
    primary = str(group.get('mentor_primary_name') or '').strip()
    secondary = str(group.get('mentor_secondary_name') or '').strip()
    if not primary and not secondary:
        return ()
    parts = []
    if primary:
        parts.append(('Mentor', primary))
    if secondary:
        parts.append(('Co-mentor', secondary))
    return tuple(parts)


def _mentor_sections_for_faculty(name, rows, groups, sections):
    """Do not claim faculty are mentors unless a section explicitly names/IDs them."""
    faculty_ids = {str(r.get('faculty_id') or '').strip().casefold() for r in rows
                   if str(r.get('faculty_id') or '').strip()}
    name_key = str(name).strip().casefold()
    assigned = []
    for section in sections:
        group = groups[section]
        for prefix in ('mentor_primary', 'mentor_secondary'):
            mentor_id = str(group.get(prefix + '_faculty_id') or '').strip().casefold()
            mentor_name = str(group.get(prefix + '_name') or '').strip().casefold()
            if ((mentor_id and mentor_id in faculty_ids) or
                    (mentor_name and mentor_name == name_key)):
                assigned.append(section)
                break
    return tuple(assigned)


def _fixed_block_row(block, days, *, mentor_names=(), mentor_sections=(), faculty_view=False, room_view=False):
    label, start, end = block
    kind = ('mentor' if 'mentor' in str(label).casefold() else
            'lunch' if 'lunch' in str(label).casefold() else 'other')
    clock = f'{start//60:02}:{start%60:02}–{end//60:02}:{end%60:02}'
    if kind == 'mentor' and mentor_names:
        description = ''.join('<small><b>' + _text(role) + ':</b> ' + _text(name) + '</small>'
                              for role, name in mentor_names)
        description += '<small class="fixed-block-footnote">Protected interval · not a scheduled teaching session</small>'
    elif kind == 'mentor' and mentor_sections:
        description = ('<small><b>Assigned mentor for:</b> '
                       + _text(', '.join(mentor_sections)) + '</small>'
                       '<small class="fixed-block-footnote">Protected interval · no teaching session created</small>')
    elif kind == 'mentor' and room_view:
        description = '<small>Protected interval · no teaching</small>'
    elif kind == 'mentor':
        description = ('<small>Protected interval · no mentor role listed for this faculty</small>'
                       if faculty_view else '<small>Mentor name not listed in workbook · protected interval</small>')
    elif kind == 'lunch':
        description = '<small>Break · no teaching</small>'
    else:
        description = '<small>Reserved · no teaching</small>'
    return ('<tr class="fixed-block-row fixed-block-row-' + kind +
            '" data-fixed-block="' + _text(label) + '">'
            '<td><span class="period-name">' + _text(label) + '</span>'
            '<span class="period-time">' + _text(clock) + '</span></td>'
            + ''.join('<td><div class="fixed-block-label"><strong>' + _text(label)
                      + '</strong>' + description + '</div></td>' for _ in days)
            + '</tr>')


def _display_rows(time_by_period, blocks):
    from timetable_policies import hm
    entries = [('period', period, hm(times[0]))
               for period,times in time_by_period.items()]
    entries.extend(('block', block, block[1]) for block in blocks)
    return [(kind, value) for kind,value,_ in sorted(entries, key=lambda item: (item[2], item[0]))]


CSS = r''' 
:root{--navy:#0b1732;--navy2:#132346;--blue:#4164ed;--cyan:#12b8ad;--muted:#65738c;
--line:#e4eaf2;--bg:#f5f7fb;--paper:#fff;--ink:#162340;--shadow:0 16px 48px rgba(21,39,75,.065)}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;color:var(--ink);background:var(--bg);font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;font-size:13px;line-height:1.5}
a{color:inherit}
.dashboard{display:grid;grid-template-columns:258px minmax(0,1fr);min-height:100vh}
.sidebar{background:linear-gradient(170deg,#101d3b 0%,#0b1732 100%);color:#fff;padding:28px 17px 24px 21px;position:sticky;top:0;height:100vh;overflow-y:auto;scrollbar-width:thin;scrollbar-color:#52648c transparent}
.brand{display:flex;gap:10px;align-items:center;padding:0 10px 27px;border-bottom:1px solid rgba(255,255,255,.10)}
.brand-icon{width:38px;height:38px;border-radius:13px;background:linear-gradient(140deg,#57d4cf,#5579ff);display:grid;place-items:center;color:#fff;font-weight:900;font-size:19px}
.brand strong{font-size:15px;letter-spacing:-.4px}.brand small{display:block;color:#aebcd8;font-size:11px;margin-top:1px}
.view-switch{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px;padding:6px;margin:17px 0 6px;background:#172746;border:1px solid #334567;border-radius:12px}
.view-tab{cursor:pointer;border:0;border-radius:8px;background:transparent;color:#bdcbea;padding:9px 6px;font:inherit;font-weight:750;font-size:11px;line-height:1.45}
.view-tab.active,.view-tab[aria-pressed="true"]{background:#3963c8;color:#fff;box-shadow:0 3px 10px #07163155}
.view-tab:focus-visible{outline:2px solid #6fe0dd;outline-offset:1px}
.dataset-strip{display:flex;flex-wrap:wrap;gap:9px;margin:0 0 15px}
.dataset-detail{flex:1 1 200px;padding:11px 15px;background:#fff;border:1px solid var(--line);border-radius:11px;min-width:0}
.dataset-detail small{display:block;text-transform:uppercase;letter-spacing:1px;color:#697d9e;font-size:9.5px;font-weight:800}
.dataset-detail strong{display:block;overflow-wrap:anywhere;color:#233b66;font-size:12px;margin-top:4px}
[hidden]{display:none!important}
.side-label{font-size:10px;letter-spacing:1.8px;font-weight:800;color:#7e95bf;margin:27px 11px 12px}
.nav-search{display:block;width:100%;padding:11px 12px;margin:0 0 10px;background:#172746;border:1px solid #324361;color:#fff;border-radius:10px;outline:none;font:inherit}
.nav-search::placeholder{color:#8c9bb4}.nav-search:focus{border-color:#65cfc8;box-shadow:0 0 0 3px #24d7c41f}
.section-nav{display:flex;flex-direction:column;gap:3px}
.nav-link{display:flex;align-items:center;justify-content:space-between;text-decoration:none;color:#bac9e4;padding:9px 12px;border-radius:9px;font-weight:600;letter-spacing:.15px;font-size:11.5px;word-break:break-word}
.nav-link:hover{background:#25385c;color:#fff}.nav-link.active{background:linear-gradient(110deg,#345bcf,#386bbb);color:#fff}
.nav-link .nav-arrow{font-size:13px;opacity:.5}.nav-link.active .nav-arrow{opacity:1}
.side-foot{font-size:11px;color:#91a4c4;margin:24px 9px;line-height:1.65;border-top:1px solid #ffffff17;padding-top:18px}
.main{padding:29px clamp(20px,3.1vw,58px) 58px;min-width:0;max-width:1700px;width:100%;margin:auto}
.eyebrow,.section-eyebrow{font-size:10.5px;font-weight:850;color:#5476dd;letter-spacing:1.7px;text-transform:uppercase}
.top-strip{display:flex;justify-content:space-between;align-items:center;gap:16px;color:#7d889b;font-size:12px;margin-bottom:14px}
.report-hero{background:linear-gradient(110deg,#112246,#182c5c 62%,#224e79);border-radius:18px;color:#fff;padding:27px 33px;position:relative;overflow:hidden;box-shadow:0 16px 40px #0a153028}
.report-hero:after{content:"";position:absolute;width:290px;height:290px;right:-65px;top:-105px;border:1px solid #ffffff32;border-radius:50%;box-shadow:0 0 0 40px #ffffff09,0 0 0 88px #ffffff05;pointer-events:none}
.report-hero .eyebrow{color:#72e5d3}.report-hero h1{font-size:clamp(24px,2.7vw,39px);line-height:1.17;letter-spacing:-1.2px;margin:10px 0}
.report-hero p{color:#c3d1e9;max-width:675px;font-size:13px;margin:0}
.hero-tags{display:flex;gap:9px;flex-wrap:wrap;margin-top:20px}
.hero-tags span{font-size:11px;padding:7px 12px;border-radius:30px;background:#ffffff17;border:1px solid #ffffff20;color:#d8e9fb}
.stat-row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:18px 0 29px}
.stat{background:#fff;border:1px solid var(--line);border-radius:14px;padding:17px 18px;box-shadow:var(--shadow)}
.stat b{display:block;font-size:26px;line-height:1.16;letter-spacing:-1px;color:#17284a}.stat small{display:block;color:#7b8aa2;margin-top:7px;font-size:11.5px}
.section-card{background:#fff;border:1px solid #e2e8f2;box-shadow:var(--shadow);border-radius:18px;margin:0 0 27px;overflow:hidden;scroll-margin-top:20px}
.section-title{padding:23px 26px 18px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:15px;flex-wrap:wrap}
.section-title h2{font-size:23px;line-height:1.2;letter-spacing:-.7px;margin:6px 0 0;overflow-wrap:anywhere}
.section-badges{display:flex;flex-wrap:wrap;gap:7px}.section-badges span{padding:6px 10px;background:#f0f5fc;border:1px solid #e0eafb;border-radius:40px;font-size:11px;color:#55709b;font-weight:700}
.grid-scroll{overflow-x:auto;padding:0 20px 14px;scrollbar-color:#ccd6e5 transparent}
.timetable{width:100%;min-width:900px;border-collapse:separate;border-spacing:0 9px;table-layout:fixed}
.timetable th{text-align:left;text-transform:uppercase;font-size:10px;letter-spacing:1.1px;color:#7888a3;padding:13px 8px 10px;font-weight:800}
.timetable th:first-child{width:105px}
.timetable td{vertical-align:top;border-top:1px solid #eef1f7;border-bottom:1px solid #eef1f7;padding:7px 5px;background:#fcfdff;overflow-wrap:anywhere}
.timetable td:first-child{border-left:1px solid #eef1f7;border-radius:8px 0 0 8px;background:#f8fafd;padding:10px 9px}
.timetable td:last-child{border-right:1px solid #eef1f7;border-radius:0 8px 8px 0}
.period-name{display:block;font-weight:800;color:#273a60}.period-time{font-size:10px;color:#8290a7;white-space:nowrap;display:block;margin-top:4px}
.day-empty{display:block;text-align:center;color:#cad4e3;font-size:20px;padding:10px}
.fixed-block-row td{background:#f2f8fc!important;border-top:1px solid #cbdfea!important;border-bottom:1px solid #cbdfea!important}
.fixed-block-row td:first-child{background:#e7f1f8!important}
.fixed-block-label{border-left:3px solid #3599b1;padding:7px 9px;background:#e7f5f8;border-radius:6px;color:#1e596a}
.fixed-block-label strong{display:block;font-size:11px}
.fixed-block-label small{display:block;color:#567f8a;font-size:9px;line-height:1.35;margin-top:4px;overflow-wrap:anywhere}
.fixed-block-label small b{font-weight:850}
.fixed-block-footnote{font-size:8.2px!important;opacity:.82}
/* Different color families for TMU's fixed mentor window and lunch break. */
.fixed-block-row-mentor td{background:#f7f3ff!important;border-color:#dcd0f3!important}
.fixed-block-row-mentor td:first-child{background:#eee8fb!important}
.fixed-block-row-mentor .fixed-block-label{background:#eee8ff;border-left-color:#7956c7;color:#473186}
.fixed-block-row-mentor .fixed-block-label small{color:#65538c}
.fixed-block-row-lunch td{background:#fffaf0!important;border-color:#f0d6a1!important}
.fixed-block-row-lunch td:first-child{background:#fff0d3!important}
.fixed-block-row-lunch .fixed-block-label{background:#fff0d3;border-left-color:#d48a21;color:#804b11}
.fixed-block-row-lunch .fixed-block-label small{color:#91652a}
.timetable-university-name{padding:18px 22px 12px;text-align:center;color:#162c55;
 font-size:clamp(20px,1.8vw,25px);font-weight:850;letter-spacing:-.35px;overflow-wrap:anywhere}
/* University stays above the header; section/faculty headings are left aligned. */
.section-card .section-title{padding-top:8px;padding-bottom:9px;border-bottom:0}
.timetable-selected-name{padding:11px 26px 15px;text-align:left;border-bottom:1px solid var(--line);color:#152746}
.timetable-selected-name h2{font-size:clamp(21px,2vw,28px);font-weight:850;line-height:1.2;margin:0;letter-spacing:-.6px;overflow-wrap:anywhere}
.open-elective-reservation{background:#eaf5f9;border:1px solid #a8dfdc;border-left:3px solid #18a99d;border-radius:9px;padding:10px 9px;margin:0 0 5px;line-height:1.35;color:#15536c}
.open-elective-reservation strong{display:block;font-size:11.5px;letter-spacing:.05px;color:#164a64;font-weight:850}
.open-elective-reservation small{display:block;font-size:9.5px;color:#487f8a;margin-top:3px}
.open-elective-reservation.has-class{padding:8px 9px;margin-bottom:5px;background:#f3fafb}
.open-elective-more{font-size:10px;color:#276783;background:#f4fafc;border:1px solid #d5e9ee;border-radius:7px;padding:7px 9px;margin-bottom:6px}
.open-elective-more summary{cursor:pointer;font-weight:750;list-style:none}
.open-elective-more summary:after{content:"  ▾";font-weight:600}
.open-elective-more[open] summary{margin-bottom:8px}
.elective-policy-error{border:2px solid #d83b52;background:#fff1f2;color:#8a1c30;border-radius:7px;padding:8px;font-weight:800;font-size:11px;margin-bottom:6px}
@media print{.open-elective-reservation{padding:5px 6px;border-width:1px}.open-elective-reservation strong{font-size:9px}}

.session{border-left:3px solid #4872e9;background:#edf2ff;padding:9px 9px 10px;border-radius:7px;margin:0 0 5px;min-width:0;box-shadow:0 1px 3px #2330480a}
.session-practical{background:#e9f8f2;border-left-color:#0cbf88}
.session-rescheduled{background:#fff0c7!important;border:2px solid #db911a!important;border-left:5px solid #ba6619!important;box-shadow:0 0 0 2px #f3c16955}
.rescheduled-flag{display:inline-block;margin:0 0 5px;padding:3px 6px;border-radius:4px;background:#8b4b0a;color:#fff;font-size:9px;font-weight:900;letter-spacing:.4px}
.session-tutorial{background:#f1edff;border-left-color:#9367e9}
.session-mentoring{background:#fff5df;border-left-color:#efab41}
.session-heading{display:flex;gap:5px;justify-content:space-between;align-items:flex-start}
.session-heading strong{font-size:11px;color:#233a70}.session-type{font-size:9px;letter-spacing:.1px;font-weight:800;color:#5773bd;background:#ffffffad;padding:2px 5px;border-radius:4px;white-space:nowrap}
.session-practical .session-type{color:#198266}.session-mentoring .session-type{color:#9e742f}
.session-title{font-size:10.8px;line-height:1.35;font-weight:650;color:#273650;margin:5px 0;padding:0;border:0;display:block}
.session-teacher{color:#495c78;font-size:10px;margin-top:6px}
.session-meta{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-top:6px;color:#3e577f;font-weight:700;font-size:9.5px}
.entry-group{display:block;color:#8a98b0;font-size:9px;margin-top:4px;overflow-wrap:anywhere}
.courses-block{border-top:1px solid #e7edf5;padding:24px 26px 28px}
.course-heading{display:flex;gap:16px;align-items:center;justify-content:space-between;margin-bottom:17px}
.course-heading h3{margin:4px 0 3px;font-size:19px;letter-spacing:-.5px}
.course-heading p{margin:2px 0;color:#7a8aa0;font-size:12px}
.count-chip{background:#e9f6f3;color:#187f76;border-radius:30px;padding:7px 12px;font-weight:750;font-size:11px;white-space:nowrap}
.course-table-wrap{border:1px solid var(--line);border-radius:12px;overflow-x:auto}
.course-table{width:100%;border-collapse:collapse;min-width:630px;font-size:12px}
.course-table th{background:#f7f9fd;color:#8290a5;letter-spacing:.65px;text-transform:uppercase;font-size:10px;padding:13px 16px;text-align:left;font-weight:800}
.course-table td{border-top:1px solid #eef1f6;padding:13px 16px;vertical-align:top;color:#3b4c67}
.course-table tbody tr:hover{background:#fbfdff}.course-code{font-weight:800;color:#2a52bd;background:#eff3ff;border-radius:5px;padding:5px 8px;display:inline-block;font-size:11px}
.course-title{font-weight:700;color:#233756!important}
.faculty-line{display:flex;align-items:flex-start;gap:7px;margin:0 0 5px}.faculty-line strong{font-size:11px;font-weight:650;display:block}.faculty-line small{font-size:10px;color:#8996a9;display:block;margin-top:1px}
.faculty-dot{width:7px;height:7px;background:#31b89d;border-radius:50%;margin-top:4px;flex-shrink:0}.type-mini-row{display:flex;gap:5px;flex-wrap:wrap}
.type-mini{font-size:10px;color:#587297;background:#eff4f9;border-radius:5px;padding:4px 6px;white-space:nowrap}
.empty-note{color:#8290a2;padding:0 26px 15px}.muted{color:#9aa8b9}
.report-footer{color:#8996aa;font-size:11px;text-align:center;padding:16px 0 12px}
@media(max-width:960px){.view-switch{max-width:430px}.nav-search{display:block}.side-label{display:block}.dashboard{display:block}.sidebar{height:auto;position:relative;padding:15px 18px}.brand{padding-bottom:10px}.side-foot{display:none}.section-nav{display:flex;flex-direction:row;overflow-x:auto;white-space:nowrap;margin:8px 0 0}.nav-link{flex-shrink:0}.main{padding:22px 15px}.stat-row{grid-template-columns:repeat(3,minmax(0,1fr))}.section-title{padding:20px}.courses-block{padding:20px}}
@media(max-width:650px){.report-hero{padding:24px}.stat-row{grid-template-columns:1fr 1fr;gap:8px}.stat{padding:12px}.stat b{font-size:23px}.section-title h2{font-size:21px}.course-heading{align-items:flex-start}}
@media print{.fixed-block-row,.fixed-block-label{-webkit-print-color-adjust:exact;print-color-adjust:exact}.timetable-university-name{font-size:17px;padding:8px 0 5px}.timetable-selected-name{padding:6px 0 8px}.timetable-selected-name h2{font-size:18px}body{background:#fff;font-size:10px}.dashboard{display:block}.sidebar,.top-strip,.report-hero,.stat-row{display:none!important}.main{padding:0!important}.section-card{page-break-before:always;box-shadow:none;margin:0 0 16px;border:0}.section-title{padding:8px}.grid-scroll{overflow:visible;padding:0}.timetable{min-width:0;font-size:9px}.timetable th,.timetable td{padding:3px}.session{padding:3px}.courses-block{padding:10px}.course-table td{padding:5px}.course-table th{padding:5px}.session-title{font-size:9px}}
'''


JS = r'''
(function(){
  const links = Array.from(document.querySelectorAll('.nav-link[data-target]'));
  const panels = Array.from(document.querySelectorAll('.timetable-panel'));
  const buttons = Array.from(document.querySelectorAll('.view-tab[data-view]'));
  const views = ['section', 'faculty', 'room'];
  let activeView = 'section';
  function setActive(target){
    links.forEach(link=>link.classList.toggle('active', link.dataset.target === target));
  }
  function showView(view, target){
    if (!views.includes(view)) return false;
    activeView = view;
    buttons.forEach(btn=>{
      const selected = btn.dataset.view === view;
      btn.classList.toggle('active', selected);
      btn.setAttribute('aria-pressed', selected ? 'true' : 'false');
    });
    document.querySelectorAll('.view-nav').forEach(nav=>{
      nav.hidden = nav.dataset.view !== view;
    });
    panels.forEach(panel=>{
      panel.hidden = panel.dataset.view !== view || (!!target && panel.id !== target);
    });
    if (target) setActive(target);
    else {
      links.forEach(link=>link.classList.remove('active'));
      const first = links.find(link=>link.closest('.view-nav')?.dataset.view === view && link.style.display !== 'none');
      if(first)first.classList.add('active');
    }
    return true;
  }
  window.switchTimetableView = function(view){
    return showView(view, null);
  };
  window.jumpToTimetableSection = function(target){
    const panel = document.getElementById(target);
    if(!panel || !panel.classList.contains('timetable-panel'))return false;
    showView(panel.dataset.view, target);
    panel.scrollIntoView({behavior:'smooth',block:'start'});
    return true;
  };
  window.focusTimetableGroup = function(group){
    const target = panels.find(p=>p.dataset.view === 'section' && p.dataset.group === group);
    showView('section', target ? target.id : null);
    if(target)target.scrollIntoView({behavior:'auto',block:'start'});
  };
  window.focusTimetableRoom = function(room){
    const target = panels.find(p=>p.dataset.view === 'room' && p.dataset.room === room);
    showView('room', target ? target.id : null);
    if(target)target.scrollIntoView({behavior:'auto',block:'start'});
  };
  window.focusTimetableFaculty = function(faculty){
    const target = panels.find(p=>p.dataset.view === 'faculty' && p.dataset.faculty === faculty);
    showView('faculty', target ? target.id : null);
    if(target)target.scrollIntoView({behavior:'auto',block:'start'});
  };
  buttons.forEach(btn=>btn.addEventListener('click',()=>showView(btn.dataset.view, null)));
  links.forEach(link=>link.addEventListener('click',function(event){
    event.preventDefault();
    window.jumpToTimetableSection(link.dataset.target);
  }));
  for(const [view, id] of [['section','sectionSearch'],['faculty','facultySearch'],['room','roomSearch']]){
    const search = document.getElementById(id);
    if(!search)continue;
    const scopedLinks = links.filter(link=>link.closest('.view-nav')?.dataset.view === view);
    search.addEventListener('input',function(){
      const q = search.value.trim().toLocaleLowerCase();
      scopedLinks.forEach(link=>{link.style.display=link.textContent.toLocaleLowerCase().includes(q)?'':'none';});
    });
  }
  showView('section', null);
})();
'''


def render_timetable_html(rows, groups, slots, reserved_elective_slot_ids=(), elective_min_semester=999,
                          elective_courses=(), elective_window_mode='NO_CLASSES_ALLOWED',
                          highlighted_session_ids=(), dataset_name=None, university_name=None,
                          timetable_type=None, fixed_non_teaching_blocks=(), rooms=()):
    """Render section, faculty and room timetables with policy-protected periods.

    The reserved windows come from Institution_Policies via the solver; they
    are a DISPLAY indicator only, never fabricated sessions in the CSV.
    """
    highlighted_session_ids = frozenset(str(x).strip() for x in highlighted_session_ids)
    days = tuple(dict.fromkeys(str(s.get('day','')).strip().upper() for s in slots if str(s.get('day','')).strip()))
    # Derive from the Excel policy engine, never from hardcoded university periods.
    reserved_elective_slot_ids = frozenset(str(sid).strip().upper() for sid in reserved_elective_slot_ids)
    elective_courses = frozenset(str(code).strip() for code in elective_courses)
    elective_window_mode = str(elective_window_mode).strip().upper()
    slot_ids_by_day_period = {}
    for slot in slots:
        try:
            day = str(slot.get('day', '')).strip().upper()
            period = int(float(slot.get('period', 0)))
        except (TypeError, ValueError):
            continue
        slot_ids_by_day_period[(day, period)] = str(slot.get('slot_id', '')).strip().upper()
    sections = sorted(g for g, r in groups.items()
                      if str(r.get('group_kind', '')).strip() == 'PARENT')
    row_by_section = _section_rows(rows, groups, sections)
    all_faculty = {str(row.get('faculty_name', '')).strip() for row in rows if row.get('faculty_name')}
    by_faculty = defaultdict(list)
    for row in rows:
        name = str(row.get('faculty_name', '')).strip()
        if name:
            by_faculty[name].append(row)
    faculty_names = sorted(by_faculty, key=str.casefold)
    room_info = {str(r.get('room_id') or '').strip():r for r in rooms if r.get('room_id')}
    by_room = defaultdict(list)
    for row in rows:
        room = str(row.get('room_id') or '').strip()
        if room:
            by_room[room].append(row)
    room_names = sorted(set(room_info) | set(by_room), key=str.casefold)
    all_courses = {str(row.get('course_id', '')).strip() for row in rows if row.get('course_id')}
    nav = []
    for i, parent in enumerate(sections):
        sec_id = f'section-{i+1:03d}'
        nav.append(f'<a href="#{sec_id}" class="nav-link" data-target="{sec_id}">'
                   f'<span>{_text(parent)}</span><span class="nav-arrow">›</span></a>')
    faculty_nav = []
    for i, name in enumerate(faculty_names):
        fid = f'faculty-{i+1:03d}'
        faculty_nav.append(f'<a href="#{fid}" class="nav-link" data-target="{fid}">'
                           f'<span>{_text(name)}</span><span class="nav-arrow">›</span></a>')
    room_nav = []
    for i, name in enumerate(room_names):
        rid = f'room-{i+1:03d}'
        room_nav.append(f'<a href="#{rid}" class="nav-link" data-target="{rid}">'
                        f'<span>{_text(name)}</span><span class="nav-arrow">›</span></a>')
    # These labels are metadata, not new scheduling constraints.
    source_label = str(dataset_name or 'Workbook name unavailable').strip()
    university_label = str(university_name or 'Not specified').strip()
    variant_label = str(timetable_type or 'Generated timetable').strip()
    metadata = [("Excel workbook", source_label), ("University", university_label),
                ("Timetable", variant_label)]
    metadata_html = ''.join(f'<div class="dataset-detail"><small>{_text(k)}</small><strong>{_text(v)}</strong></div>'
                            for k, v in metadata)
    out = [
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<title>Timora Workspace | Weekly Schedules</title>',
        f'<style>{CSS}</style></head><body>',
        '<div class="dashboard"><aside class="sidebar" aria-label="Timetable navigation">',
        '<div class="brand"><span class="brand-icon">T</span><span><strong>Timora Workspace</strong><small>Academic planning workspace</small></span></div>',
        '<div class="view-switch" role="group" aria-label="Timetable view">'
        '<button type="button" class="view-tab active" data-view="section" aria-pressed="true">Section-wise</button>'
        '<button type="button" class="view-tab" data-view="faculty" aria-pressed="false">Faculty-wise</button><button type="button" class="view-tab" data-view="room" aria-pressed="false">Room-wise</button></div>',
        '<div class="view-nav" data-view="section"><div class="side-label">BROWSE SECTIONS</div>'
        '<input id="sectionSearch" class="nav-search" placeholder="Search a section..." aria-label="Search section">',
        f'<nav class="section-nav">{"".join(nav)}</nav></div>',
        '<div class="view-nav" data-view="faculty" hidden><div class="side-label">BROWSE FACULTY</div>'
        '<input id="facultySearch" class="nav-search" placeholder="Search faculty..." aria-label="Search faculty">',
        f'<nav class="section-nav">{"".join(faculty_nav)}</nav></div>',
        '<div class="view-nav" data-view="room" hidden><div class="side-label">BROWSE ROOMS</div>'
        '<input id="roomSearch" class="nav-search" placeholder="Search a room..." aria-label="Search room">',
        f'<nav class="section-nav">{"".join(room_nav)}</nav></div>',
        '<div class="side-foot">Switch between section-wise, faculty-wise and room-wise schedules. Search or choose an entry to focus its timetable.</div>',
        '</aside><main class="main">',
        '<div class="top-strip"><span>ACADEMIC SCHEDULING / VERIFIED WEEKLY PLAN</span><span>Interactive timetable report</span></div>',
        f'<div class="dataset-strip" aria-label="Active dataset details">{metadata_html}</div>',
        '<header class="report-hero"><div class="eyebrow">AI-ASSISTED SCHEDULING</div>',
        '<h1>A clearer view of your academic week.</h1>',
        '<p>Browse validated section, faculty and room schedules, room use, practical blocks and academic assignments.</p>',
        '<div class="hero-tags"><span>Weekly schedule</span><span>Section-wise</span><span>Faculty-wise</span><span>Room-wise</span></div></header>',
        '<div class="stat-row">',
        f'<div class="stat"><b>{len(sections)}</b><small>Academic sections</small></div>',
        f'<div class="stat"><b>{len(all_courses)}</b><small>Scheduled courses</small></div>',
        f'<div class="stat"><b>{len(all_faculty)}</b><small>Teaching faculty</small></div>',
        '</div>',
    ]
    if highlighted_session_ids:
        out.append('<div class="updated-legend" style="margin:0 0 15px;padding:12px;border-radius:9px;background:#fff0c7;border-left:5px solid #ba6619;color:#713e10;font-weight:700">Gold cards highlight classes changed in this Updated Timetable.</div>')
    time_by_period = {}
    for slot in slots:
        try:
            period = int(float(slot.get('period', 0)))
        except (TypeError, ValueError):
            continue
        if period and period not in time_by_period:
            time_by_period[period] = (str(slot.get('start_time', '')),
                                     str(slot.get('end_time', '')))
    all_periods = sorted(time_by_period) or list(range(1, 9))
    display_rows = _display_rows(time_by_period, fixed_non_teaching_blocks)
    for i, parent in enumerate(sections):
        section_rows = row_by_section[parent]
        try:
            is_elective_eligible = int(float(groups[parent].get('semester', 0))) >= int(elective_min_semester)
        except (TypeError, ValueError):
            is_elective_eligible = False
        by_slot = defaultdict(list)
        for row in section_rows:
            try:
                period = int(float(row.get('period', 0)))
            except (TypeError, ValueError):
                continue
            by_slot[(str(row.get('day', '')), period)].append(row)
        section_id = f'section-{i+1:03d}'
        faculty_count = len({r.get('faculty_name') for r in section_rows if r.get('faculty_name')})
        course_count = len({r.get('course_id') for r in section_rows if r.get('course_id')})
        session_count = len({r.get('session_id') for r in section_rows if r.get('session_id')})
        program = str(groups[parent].get('program', '')).strip()
        sem = str(groups[parent].get('semester', '')).strip()
        badges = [f'<span>{course_count} courses</span>',f'<span>{faculty_count} faculty</span>',
                  f'<span>{session_count} weekly sessions</span>']
        if program:
            badges.insert(0, f'<span>{_text(program)}</span>')
        if sem:
            badges.insert(1 if program else 0, f'<span>Semester {_text(sem)}</span>')
        out.append(f'<section class="section-card timetable-panel" id="{section_id}" data-view="section" data-group="{_text(parent)}">')
        out.append(_university_heading(university_label))
        out.append(f'<div class="section-title"><div><div class="section-eyebrow">WEEKLY TIMETABLE · SECTION {i+1:02d}</div>'
                   f'</div><div class="section-badges">{"".join(badges)}</div></div>')
        out.append(f'<div class="timetable-selected-name" aria-label="Selected section"><h2>{_text(parent)}</h2></div>')
        out.append('<div class="grid-scroll"><table class="timetable"><thead><tr><th>Time slot</th>')
        out.extend(f'<th>{DAY_NAMES.get(day, day.title())}</th>' for day in days)
        out.append('</tr></thead><tbody>')
        for row_type, payload in display_rows:
            if row_type == 'block':
                out.append(_fixed_block_row(payload, days, mentor_names=_mentor_names(groups[parent])))
                continue
            period = payload
            start, end = time_by_period.get(period, ('', ''))
            label_time = f'{_text(start)}–{_text(end)}' if start or end else ''
            out.append('<tr><td><span class="period-name">Period ' + str(period) + '</span>'
                       f'<span class="period-time">{label_time}</span></td>')
            for day in days:
                entries = sorted(by_slot.get((day, period), []),
                                 key=lambda r: (str(r.get('course_id', '')),str(r.get('group_id', ''))))
                # The reservation banner is presentation only. Never hide a
                # violating session; the independent validator still rejects it.
                sid = slot_ids_by_day_period.get((day, period))
                in_reservation = is_elective_eligible and sid in reserved_elective_slot_ids
                if in_reservation and elective_window_mode != 'DISABLED':
                    empty = elective_window_mode == 'NO_CLASSES_ALLOWED'
                    detail = ('Reserved — no scheduled classes' if empty
                              else 'Reserved for declared Open Electives')
                    banner = ('<div class="open-elective-reservation">'
                              '<strong>Open Elective</strong>'
                              f'<small>{_text(detail)}</small></div>')
                    violations = [r for r in entries if empty or str(r.get('course_id','')) not in elective_courses]
                    cells = banner
                    if violations:
                        cells += ('<div class="elective-policy-error">POLICY VIOLATION: '
                                  'A forbidden class occupies this reserved period</div>')
                    if entries:
                        cells += ''.join(_entry_card(r, highlighted_session_ids) for r in entries)
                else:
                    cells = ''.join(_entry_card(r, highlighted_session_ids) for r in entries) if entries else '<span class="day-empty">–</span>'
                out.append(f'<td>{cells}</td>')
            out.append('</tr>')
        out.append('</tbody></table></div>')
        out.append('<div class="courses-block">')
        out.append(_course_table(section_rows))
        out.append('</div></section>')
    for i, name in enumerate(faculty_names):
        assigned_sections = _mentor_sections_for_faculty(name, by_faculty[name], groups, sections)
        out.append(_faculty_panel(name, by_faculty[name], days, display_rows,
                                  time_by_period, highlighted_session_ids, i + 1,
                                  university_name=university_label, mentor_sections=assigned_sections))
    for i, name in enumerate(room_names):
        out.append(_room_panel(name, by_room[name], days, display_rows, time_by_period,
                               highlighted_session_ids, i + 1, university_name=university_label,
                               room_info=room_info.get(name)))
    out.append('<footer class="report-footer">Automatically generated weekly timetable. '
               'Check the validation report for data provenance and prototype resources.</footer>')
    out.append('</main></div>')
    out.append(f'<script>{JS}</script></body></html>')
    return '\n'.join(out)
