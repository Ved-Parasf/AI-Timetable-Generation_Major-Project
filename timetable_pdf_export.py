"""Printable, whole-university PDF timetable exports from an existing verified CSV.

Presentation only: this module NEVER invokes CP-SAT, changes an assignment,
or declares a new timetable valid. Call only after independent validation.
"""
from __future__ import annotations

import csv
import html
import io
from collections import defaultdict

from timetable_report import _section_rows, _mentor_names, DAY_NAMES


def _text(value):
    return html.escape(str(value if value is not None else ''), quote=False)


def _period(row):
    try:
        return int(float(row.get('period') or 0))
    except (ValueError, TypeError):
        return 0


def _by_clock(block):
    return f'{block[1]//60:02}:{block[1]%60:02}-{block[2]//60:02}:{block[2]%60:02}'


def _pdf_bytes(rows, records, policies, *, university_name, dataset_name, version_name, types,
               highlighted_session_ids=()):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, PageBreak, KeepTogether
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import os
    import reportlab

    try:
        # ReportLab ships these fonts; no external font file or OS-specific path needed.
        font_dir = os.path.join(os.path.dirname(reportlab.__file__), 'fonts')
        if 'TimoraVera' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont('TimoraVera', os.path.join(font_dir, 'Vera.ttf')))
        if 'TimoraVeraBd' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont('TimoraVeraBd', os.path.join(font_dir, 'VeraBd.ttf')))
        regular, bold = 'TimoraVera', 'TimoraVeraBd'
    except (OSError, ValueError):
        regular, bold = 'Helvetica', 'Helvetica-Bold'
    navy, blue, ink = colors.HexColor('#172c52'), colors.HexColor('#365dc4'), colors.HexColor('#243a5e')
    pale = colors.HexColor('#f1f5fc')
    styles = {
        'heading': ParagraphStyle('TimoraHeading', fontName=bold, fontSize=14, leading=18, textColor=navy, spaceAfter=7),
        'name': ParagraphStyle('TimoraName', fontName=bold, fontSize=12, leading=15, textColor=blue, spaceAfter=5),
        'small': ParagraphStyle('TimoraSmall', fontName=regular, fontSize=6.7, leading=9, textColor=ink),
        'cell': ParagraphStyle('TimoraCell', fontName=regular, fontSize=6.15, leading=8.1, textColor=ink, alignment=TA_LEFT, splitLongWords=True),
        'label': ParagraphStyle('TimoraLabel', fontName=bold, fontSize=7.2, leading=9, textColor=navy),
        'white': ParagraphStyle('TimoraWhite', fontName=bold, fontSize=7.2, leading=9, textColor=colors.white),
    }
    buffer = io.BytesIO()
    page_w, page_h = landscape(A4)
    doc = SimpleDocTemplate(buffer, pagesize=(page_w, page_h),
                            leftMargin=13*mm, rightMargin=13*mm,
                            topMargin=13*mm, bottomMargin=15*mm, title=f'{version_name} - Timora Timetables',
                            author='Timora Workspace')
    def para(value, style='cell'):
        return Paragraph(value, styles[style])

    groups = {str(g.get('group_id') or '').strip():g for g in records.get('Student_Groups', [])
              if g.get('group_id')}
    sections = sorted(g for g, r in groups.items() if str(r.get('group_kind', '')).strip() == 'PARENT')
    section_rows = _section_rows(rows, groups, sections)
    faculty_rows = defaultdict(list)
    room_rows = defaultdict(list)
    for row in rows:
        teacher = str(row.get('faculty_name') or '').strip()
        room = str(row.get('room_id') or '').strip()
        if teacher:
            faculty_rows[teacher].append(row)
        if room:
            room_rows[room].append(row)
    room_info = {str(r.get('room_id') or '').strip():r for r in records.get('Rooms', [])
                 if str(r.get('room_id') or '').strip()}
    room_names = sorted(set(room_info) | set(room_rows), key=str.casefold)

    slots = records.get('Time_Slots', [])
    days = list(dict.fromkeys(str(s.get('day') or '').strip().upper() for s in slots if s.get('day')))
    periods = sorted({_period(s) for s in slots if _period(s) > 0})
    slot_id_by_day_period = {
        (str(slot.get('day') or '').strip().upper(), _period(slot)):
            str(slot.get('slot_id') or '').strip().upper()
        for slot in slots if _period(slot) > 0
    }
    reserved = {str(x).strip().upper() for x in getattr(policies, 'reserved_slot_ids', ())}
    mode = str(getattr(policies, 'elective_window_mode', 'DISABLED')).upper()
    minimum_semester = getattr(policies, 'elective_min_sem', 999)
    clock = {}
    for s in slots:
        p = _period(s)
        if p and p not in clock:
            clock[p] = (str(s.get('start_time') or '').strip(), str(s.get('end_time') or '').strip())
    fixed = list(getattr(policies, 'fixed_non_teaching_blocks', ()) or ())
    chronological = [('period', p, clock.get(p, ('', ''))[0]) for p in periods]
    # The same order as the HTML display, including policy-protected intervals.
    from timetable_policies import hm
    chronological = [('period', p, hm(clock[p][0])) for p in periods if p in clock]
    chronological += [('block', b, int(b[1])) for b in fixed]
    chronological.sort(key=lambda x: (x[2], x[0]))

    all_groups = {
        'section': [(name, section_rows[name]) for name in sections],
        'faculty': [(name, faculty_rows[name]) for name in sorted(faculty_rows, key=str.casefold)],
        'room': [(name, room_rows[name]) for name in room_names],
    }
    titles = {'section': 'Section-wise timetable', 'faculty': 'Faculty-wise timetable', 'room': 'Room-wise timetable'}

    moved_ids = {str(v).strip() for v in highlighted_session_ids}

    def cell_for(r, typ):
        moved = str(r.get('session_id') or '').strip() in moved_ids
        kind = str(r.get('kind') or '').strip().upper()
        code = str(r.get('course_id') or '').strip() or 'Course'
        teacher = str(r.get('faculty_name') or '').strip()
        group = str(r.get('group_id') or '').strip()
        room = str(r.get('room_id') or '').strip()
        secondary = {'section': [teacher, room, group],
                     'faculty': [group, room],
                     'room': [group, teacher]}[typ]
        secondary = [x for x in secondary if x]
        return (('<font color="#99520b"><b>MOVED</b></font><br/>' if moved else '')
                + '<b>' + _text(code) + '</b>' + (' (' + _text(kind.title()) + ')' if kind else '')
                + '<br/>' + '<br/>'.join(_text(x) for x in secondary))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#d9e2f1'))
        canvas.line(13*mm, 11*mm, page_w-13*mm, 11*mm)
        canvas.setFillColor(colors.HexColor('#687c9d'))
        canvas.setFont(regular, 7.5)
        canvas.drawString(13*mm, 7.5*mm, 'Timora | From selected independently validated timetable | Not a new solve')
        canvas.drawRightString(page_w-13*mm, 7.5*mm, f'Page {document.page}')
        canvas.restoreState()

    all_items = [(typ, name, items) for typ in types for name, items in all_groups[typ]]
    if not all_items:
        raise ValueError('No sections, faculty or rooms are available to export.')
    story = []
    usable = page_w - doc.leftMargin - doc.rightMargin
    widths = [101] + [(usable-101)/max(len(days), 1)]*len(days)
    for i, (typ, name, items) in enumerate(all_items):
        if i:
            story.append(PageBreak())
        story.append(para(_text(university_name or 'University not specified'), 'heading'))
        story.append(para(_text(name), 'name'))
        details = f'{titles[typ]} | {version_name} | Workbook: {dataset_name}'
        if typ == 'room' and name in room_info:
            details += ' | Room type: ' + str(room_info[name].get('room_type') or 'Not specified')
        story.append(para(_text(details), 'small'))
        story.append(Spacer(1, 9))
        slots_by_day = defaultdict(list)
        for row in items:
            p = _period(row)
            if p:
                slots_by_day[(str(row.get('day') or '').strip().upper(), p)].append(row)
        header = [para('Time slot', 'white')]+[para(_text(DAY_NAMES.get(day, day.title())), 'white') for day in days]
        data = [header]
        row_kinds = []
        for row_kind, payload, _ in chronological:
            row_kinds.append(row_kind)
            if row_kind == 'block':
                label, start, end = payload
                entry = para('<b>' + _text(label) + '</b><br/>' + _text(_by_clock(payload)), 'cell')
                block_text = _text(label)
                if 'mentor' in str(label).lower() and typ == 'section':
                    names = _mentor_names(groups[name])
                    if names:
                        block_text += '<br/>' + '<br/>'.join(_text(role + ': ' + nm) for role,nm in names)
                if 'mentor' in str(label).lower() and typ == 'faculty':
                    mentor_groups = [group for group in sections
                                     if any(nm.strip().casefold() == name.casefold()
                                            for _role,nm in _mentor_names(groups[group]))]
                    if mentor_groups:
                        block_text += '<br/>Mentor for: ' + _text(', '.join(mentor_groups))
                data.append([entry] + [para(block_text) for _ in days])
                continue
            period = payload
            start,end = clock.get(period, ('',''))
            cells = [para(f'<b>Period {period}</b><br/>' + _text(start + '-' + end))]
            for day in days:
                sessions = sorted(slots_by_day.get((day,period), []),
                                  key=lambda r: (str(r.get('course_id','')),str(r.get('group_id',''))))
                value = '<br/><br/>'.join(cell_for(r, typ) for r in sessions) if sessions else '-'
                if typ == 'section' and mode != 'DISABLED':
                    try:
                        eligible = int(float(groups[name].get('semester') or 0)) >= int(minimum_semester)
                    except (TypeError, ValueError):
                        eligible = False
                    if eligible and slot_id_by_day_period.get((day,period)) in reserved:
                        value = '<b>Open Elective reserved window</b>' + (('<br/>' + value) if sessions else '')
                cells.append(para(value))
            data.append(cells)
        t = Table(data, colWidths=widths, repeatRows=1, hAlign='LEFT')
        cmds = [('BACKGROUND', (0,0),(-1,0),navy),
                ('VALIGN',(0,0),(-1,-1),'TOP'),
                ('GRID',(0,0),(-1,-1),0.38,colors.HexColor('#dfe6f0')),
                ('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),5),
                ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)]
        for j, row_kind in enumerate(row_kinds, start=1):
            if row_kind == 'block':
                label = str(chronological[j-1][1][0]).lower()
                bg = '#f1eafa' if 'mentor' in label else '#fff0d5' if 'lunch' in label else '#e8f1f9'
                cmds.append(('BACKGROUND',(0,j),(-1,j),colors.HexColor(bg)))
            elif j % 2 == 0:
                cmds.append(('BACKGROUND',(0,j),(-1,j),pale))
        t.setStyle(TableStyle(cmds))
        story.append(t)
        story.append(Spacer(1, 9))
        story.append(para('Displays every scheduled period from the selected run. Empty cells mean no assigned '
                          'session in this view; they do not guarantee rescheduling feasibility.', 'small'))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def build_pdf_exports(csv_blob: bytes, workbook_blob: bytes, *, university_name='',
                      dataset_name='', version_name='Generated timetable',
                      highlighted_session_ids=()) -> dict[str,bytes]:
    """Four complete, tabular PDFs: all sections, all faculty, all rooms, combined."""
    from northcap_cp_sat_solver import excel_records
    from timetable_policies import load_policies
    records = excel_records(io.BytesIO(workbook_blob))
    policies = load_policies(records)
    if policies.errors:
        raise ValueError('The workbook contains unsupported or inconsistent scheduling policies.')
    text = csv_blob.decode('utf-8-sig', errors='replace')
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError('The selected timetable has no sessions to export.')
    args = dict(university_name=university_name, dataset_name=dataset_name,
                version_name=version_name, highlighted_session_ids=highlighted_session_ids)
    return {
        'all_sections.pdf':_pdf_bytes(rows, records, policies, types=('section',), **args),
        'all_faculty.pdf':_pdf_bytes(rows, records, policies, types=('faculty',), **args),
        'all_rooms.pdf':_pdf_bytes(rows, records, policies, types=('room',), **args),
        'all_three_views.pdf':_pdf_bytes(rows, records, policies, types=('section','faculty','room'), **args),
    }
