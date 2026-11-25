"""Durable local timetable library for Timora.

Validated versions remain in timetable_records SQLite. Workbooks are persisted
alongside them so the reports can be reconstructed after Streamlit reloads.
No user authentication or cloud backup is provided by this prototype.
"""
from __future__ import annotations
import csv
import io
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from timetable_records import connect, data_digest, list_versions, get_version, add_version, utcnow
from timetable_rescheduling import csv_rows

PASS = ('PASS', 'PASS_WITH_LIMITATIONS')


def save_workbook(workbook_blob: bytes, filename: str, university_name: str = '') -> str:
    if not workbook_blob or not filename.lower().endswith('.xlsx'):
        raise ValueError('A valid Excel workbook is required')
    digest = data_digest(workbook_blob)
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS persisted_workbooks (
          dataset_hash TEXT PRIMARY KEY, filename TEXT NOT NULL,
          university_name TEXT NOT NULL, workbook_blob BLOB NOT NULL,
          updated_at TEXT NOT NULL)''')
        db.execute('''INSERT INTO persisted_workbooks
            (dataset_hash, filename, university_name, workbook_blob, updated_at)
            VALUES (?,?,?,?,?) ON CONFLICT(dataset_hash) DO UPDATE SET
            filename=excluded.filename, university_name=excluded.university_name,
            workbook_blob=excluded.workbook_blob, updated_at=excluded.updated_at''',
            (digest, filename, university_name, workbook_blob, utcnow()))
    return digest


def saved_datasets() -> list[dict]:
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS persisted_workbooks (
          dataset_hash TEXT PRIMARY KEY, filename TEXT NOT NULL,
          university_name TEXT NOT NULL, workbook_blob BLOB NOT NULL,
          updated_at TEXT NOT NULL)''')
        return [dict(r) for r in db.execute('''SELECT p.dataset_hash,p.filename,p.university_name,
             p.updated_at,(SELECT COUNT(*) FROM versions WHERE dataset_hash=p.dataset_hash) version_count
             FROM persisted_workbooks p ORDER BY updated_at DESC''')]


def read_workbook(digest: str) -> dict | None:
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS persisted_workbooks (
          dataset_hash TEXT PRIMARY KEY, filename TEXT NOT NULL,
          university_name TEXT NOT NULL, workbook_blob BLOB NOT NULL,
          updated_at TEXT NOT NULL)''')
        item = db.execute('SELECT * FROM persisted_workbooks WHERE dataset_hash=?', (digest,)).fetchone()
        return dict(item) if item else None


def archive_validated_result(workbook_blob: bytes, result: dict) -> list[str]:
    """Save only independently validated outputs; never manufacture Fairness.

    Standard baseline is saved even when the main result is a fairness solve.
    Approved moves are committed by existing approval transactions.
    """
    versions = []
    from timetable_versions import available_versions
    for option in available_versions(result):
        if option['kind'] not in ('STANDARD', 'FAIRNESS'):
            continue
        if option['verified_status'] not in PASS:
            continue
        metrics = {'include_mentor': result.get('include_mentor', True),
                   'sim_count': result.get('sim_count', 0),
                   'university_name': result.get('university_name', ''),
                   'input_file': result.get('input_file', ''),
                   'source': 'automatically saved validated timetable'}
        # Preserve this run's measured UCB1 result with the immutable Fairness
        # archive, so the results dashboard survives a browser restart.
        if option['kind'] == 'FAIRNESS' and result.get('fairness_alns_enabled'):
            report = result.get('files', {}).get('alns_learning_report.json')
            if report:
                try:
                    parsed = json.loads(report)
                    if (isinstance(parsed, dict)
                            and isinstance(parsed.get('final_change_audit'), dict)
                            and isinstance(parsed.get('rounds'), list)):
                        metrics['alns_report'] = parsed
                except (ValueError, TypeError, UnicodeError):
                    pass
        versions.append(add_version(data_digest(workbook_blob), option['csv'],
                                    option['kind'], metrics))
    return versions


def restore_version(workbook_entry: dict, version_id: str) -> dict:
    """Revalidate saved CSV and rebuild live report/section/faculty/room artifacts."""
    from northcap_cp_sat_solver import excel_records
    from timetable_validator import validate_bytes
    from timetable_workflows_ui import rebuild_verified_artifacts
    workbook = workbook_entry['workbook_blob']
    stored = get_version(data_digest(workbook), version_id)
    if not stored:
        raise ValueError('Saved version does not belong to the selected workbook')
    metadata = json.loads(stored.get('metrics_json') or '{}')
    include_mentor = bool(metadata.get('include_mentor', True))
    sim_count = int(metadata.get('sim_count', 0))
    verified = validate_bytes(workbook, stored['csv_blob'], include_mentor, sim_count)
    if verified.get('status') not in PASS:
        raise ValueError('Archived timetable failed independent validation; it was not opened')
    data = excel_records(io.BytesIO(workbook))
    is_approved = stored['variant'] in ('COORDINATOR_APPROVED',)
    label = ('Updated Timetable' if is_approved else 'Fairness optimized'
             if stored['variant'] == 'FAIRNESS' else 'Standard CP-SAT')
    marked = metadata.get('highlighted_session_ids', []) if is_approved else []
    files, _ = rebuild_verified_artifacts(
        data, csv_rows(stored['csv_blob']), include_mentoring=include_mentor,
        extra_labs=sim_count, highlighted_session_ids=marked,
        quality_mode='restored_archive', dataset_name=workbook_entry['filename'],
        university_name=workbook_entry['university_name'], timetable_type=label)
    alns_report = metadata.get('alns_report') if stored['variant'] == 'FAIRNESS' else None
    if (isinstance(alns_report, dict)
            and isinstance(alns_report.get('final_change_audit'), dict)
            and isinstance(alns_report.get('rounds'), list)):
        from timetable_bandit_alns import final_change_csv
        files['alns_learning_report.json'] = json.dumps(alns_report, indent=2).encode('utf-8')
        files['alns_final_changes.csv'] = final_change_csv(
            alns_report.get('final_changed_sessions', []))
        stream = io.StringIO()
        if alns_report['rounds']:
            writer = csv.DictWriter(stream, fieldnames=list(alns_report['rounds'][0]),
                                    extrasaction='ignore')
            writer.writeheader()
            writer.writerows(alns_report['rounds'])
        files['alns_learning_rounds.csv'] = stream.getvalue().encode('utf-8-sig')
    result = {'files': files, 'status_code': 0, 'independent_status': verified['status'],
              'fairness_alns_enabled': bool(alns_report),
              'baseline_independent_status': 'NOT_RUN', 'fairness_enabled': stored['variant'] == 'FAIRNESS',
              'fairness_requested': stored['variant'] == 'FAIRNESS',
              'rescheduled': is_approved, 'include_mentor': include_mentor,
              'sim_count': sim_count, 'input_file': workbook_entry['filename'],
              'university_name': workbook_entry['university_name'],
              'highlighted_session_ids': marked, 'restored_saved_version': version_id,
              'logs': 'Restored saved version '+version_id+'; independently validated again. No new solve performed.'}
    return result


def saved_pdf_exports(workbook_entry: dict, version_id: str) -> dict[str,bytes]:
    """Generate printable views from the immutable validated archived CSV."""
    from timetable_pdf_export import build_pdf_exports
    saved = get_version(workbook_entry['dataset_hash'], version_id)
    if not saved:
        raise ValueError('Archived timetable unavailable')
    from timetable_validator import validate_bytes
    metadata = json.loads(saved.get('metrics_json') or '{}')
    verified = validate_bytes(workbook_entry['workbook_blob'], saved['csv_blob'],
                              metadata.get('include_mentor', True), metadata.get('sim_count', 0))
    if verified.get('status') not in PASS:
        raise ValueError('Saved version failed independent validation')
    return build_pdf_exports(saved['csv_blob'], workbook_entry['workbook_blob'],
        university_name=workbook_entry['university_name'],
        dataset_name=workbook_entry['filename'], version_name=saved['variant'],
        highlighted_session_ids=tuple(metadata.get('highlighted_session_ids', [])))


def version_package(workbook_entry: dict, version_id: str) -> bytes:
    """One ZIP with the three independently regenerated PDF timetable files.

    Also includes the archived source CSV; no rescheduling is performed.
    """
    saved = get_version(workbook_entry['dataset_hash'], version_id)
    if not saved:
        raise ValueError('Missing saved timetable')
    pdfs = saved_pdf_exports(workbook_entry, version_id)
    buffer = io.BytesIO()
    with ZipFile(buffer, 'w', ZIP_DEFLATED) as z:
        z.writestr('timetable_all_sessions.csv', saved['csv_blob'])
        for key, name in (('all_sections.pdf','classwise.pdf'),
                          ('all_faculty.pdf','facultywise.pdf'),
                          ('all_rooms.pdf','roomwise.pdf')):
            z.writestr(name, pdfs[key])
    return buffer.getvalue()


def saved_exports_root() -> Path:
    """Persistent export folder lives next to the permanent SQLite DB."""
    import os
    from timetable_records import db_path
    configured = os.environ.get('TIMORA_SAVED_EXPORTS_PATH')
    return Path(configured) if configured else db_path().parent / 'saved_timetables'


def ensure_version_files(workbook_entry: dict, version_id: str) -> Path:
    """Write durable per-version PDF and CSV files from verified archived data.

    Independent validation happens before PDF generation; archived originals are
    never edited. Existing exports are reused, avoiding repeat generation.
    """
    from timetable_records import get_version
    saved = get_version(workbook_entry['dataset_hash'], version_id)
    if not saved:
        raise ValueError('Saved timetable is not available')
    variant = str(saved['variant'])
    if not variant.replace('_', '').isalnum():
        raise ValueError('Unexpected saved timetable variant')
    folder = (saved_exports_root() / workbook_entry['dataset_hash'] /
              variant / version_id)
    metadata = json.loads(saved.get('metrics_json') or '{}')
    alns_report = metadata.get('alns_report') if variant == 'FAIRNESS' else None
    expected = ('classwise.pdf', 'facultywise.pdf', 'roomwise.pdf',
                'classwise.csv', 'facultywise.csv', 'roomwise.csv')
    if isinstance(alns_report, dict) and 'final_change_audit' in alns_report:
        expected += ('ucb1_final_changes.csv', 'ucb1_learning_report.json')
    if all((folder / name).exists() and (folder / name).stat().st_size > 0
           for name in expected):
        return folder
    pdfs = saved_pdf_exports(workbook_entry, version_id)
    rows = csv_rows(saved['csv_blob'])
    folder.mkdir(parents=True, exist_ok=True)
    for name, pdfkey in [('classwise.pdf', 'all_sections.pdf'),
                         ('facultywise.pdf', 'all_faculty.pdf'),
                         ('roomwise.pdf', 'all_rooms.pdf')]:
        (folder / name).write_bytes(pdfs[pdfkey])
    # The three CSVs contain the same canonical sessions, sorted for browsing
    # rather than pretending that each is a separate scheduling result.
    fields = list(rows[0]) if rows else []
    for filename, grouping in [('classwise.csv', 'group_id'),
                               ('facultywise.csv', 'faculty_name'),
                               ('roomwise.csv', 'room_id')]:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (
            str(r.get(grouping, '')).casefold(),
            str(r.get('day', '')).casefold(),
            int(float(r.get('period') or 0)))))
        (folder / filename).write_bytes(buffer.getvalue().encode('utf-8-sig'))
    (folder / 'timetable_all.csv').write_bytes(saved['csv_blob'])
    if isinstance(alns_report, dict) and 'final_change_audit' in alns_report:
        from timetable_bandit_alns import final_change_csv
        (folder / 'ucb1_final_changes.csv').write_bytes(
            final_change_csv(alns_report.get('final_changed_sessions', [])))
        (folder / 'ucb1_learning_report.json').write_text(
            json.dumps(alns_report, indent=2), encoding='utf-8')
    (folder / 'manifest.json').write_text(json.dumps({
        'version_id': version_id, 'variant': variant,
        'university': workbook_entry['university_name'],
        'workbook': workbook_entry['filename'],
        'validation': 'rechecked before export',
        'note': 'All files refer to one identical immutable saved timetable version.'
    }, indent=2), encoding='utf-8')
    return folder
