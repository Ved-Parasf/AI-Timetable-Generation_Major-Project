"""Persistent validated timetables for Timora (local, single-installation prototype).

Store original Excel in SQLite next to immutable timetable version snapshots; keep
three complete PDF views (and corresponding CSVs) in a stable data directory.
Never run CP-SAT or replace the uploaded workbook when reopening an archive.

This is local disk persistence, *not* university authentication or cloud backup.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from timetable_records import (connect, db_path, data_digest, add_version, get_version,
                               list_versions, utcnow)
from timetable_rescheduling import schedule_hash, csv_rows
from timetable_versions import available_versions
from timetable_validator import validate_bytes

PASS = frozenset(('PASS', 'PASS_WITH_LIMITATIONS'))
PDF_NAMES = ('all_sections.pdf', 'all_faculty.pdf', 'all_rooms.pdf', 'all_three_views.pdf')
CSV_NAMES = ('timetable_groups.csv', 'timetable_faculty.csv', 'timetable_rooms.csv', 'timetable_all.csv')
_SAVED_FILE_NAMES = frozenset({
    'timetable_all.csv', 'timetable_groups.csv', 'timetable_faculty.csv',
    'timetable_rooms.csv', 'timetable_sections.html',
    'quality_summary.json', 'quality_summary.txt',
    'independent_validation.json', 'independent_validation.txt',
    'validation_report.txt', 'policy_compliance.json', 'policy_compliance.txt',
    'summary.txt', 'solver_log.txt',
})


def _prepare() -> None:
    with connect() as db:
        db.executescript('''
          CREATE TABLE IF NOT EXISTS timora_saved_projects (
            dataset_hash TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            university TEXT NOT NULL,
            workbook_blob BLOB NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS timora_saved_version_info (
            version_id TEXT PRIMARY KEY REFERENCES versions(version_id) ON DELETE CASCADE,
            dataset_hash TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
          );
        ''')


def save_project(workbook: bytes, filename: str, university: str) -> str:
    """Remember the uploaded ORIGINAL Excel, not a rewritten/normalized copy."""
    if not isinstance(workbook, bytes) or not workbook.startswith(b'PK'):
        raise ValueError('Expected the original Excel .xlsx file bytes')
    digest = data_digest(workbook)
    _prepare()
    with connect() as db:
        db.execute('''INSERT INTO timora_saved_projects
            (dataset_hash,filename,university,workbook_blob,created_at,updated_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(dataset_hash) DO UPDATE SET
            filename=excluded.filename, university=excluded.university,
            updated_at=excluded.updated_at''',
            (digest, str(filename or 'university.xlsx')[:250],
             str(university or 'University not specified')[:250],
             workbook, utcnow(), utcnow()))
    return digest


def saved_projects() -> list[dict]:
    """Only list projects that have at least one archived timetable."""
    _prepare()
    with connect() as db:
        return [dict(r) for r in db.execute('''
          SELECT p.dataset_hash, p.filename, p.university, p.updated_at,
                 COUNT(v.version_id) AS number_of_versions,
                 MAX(v.created_at) AS latest_version_at
          FROM timora_saved_projects p
          JOIN versions v ON v.dataset_hash=p.dataset_hash
          GROUP BY p.dataset_hash
          ORDER BY latest_version_at DESC, p.updated_at DESC
        ''')]


def saved_project(dataset_hash: str) -> dict | None:
    _prepare()
    with connect() as db:
        record = db.execute('SELECT * FROM timora_saved_projects WHERE dataset_hash=?',
                            (dataset_hash,)).fetchone()
        return dict(record) if record else None


def _update_version_info(dataset_hash: str, version_id: str, settings: dict):
    _prepare()
    with connect() as db:
        db.execute('''INSERT INTO timora_saved_version_info
             (version_id,dataset_hash,settings_json,updated_at) VALUES (?,?,?,?)
             ON CONFLICT(version_id) DO UPDATE SET
             settings_json=excluded.settings_json, updated_at=excluded.updated_at''',
             (version_id, dataset_hash, json.dumps(settings, sort_keys=True), utcnow()))


def version_info(version_id: str) -> dict:
    _prepare()
    with connect() as db:
        row = db.execute('SELECT settings_json FROM timora_saved_version_info WHERE version_id=?',
                         (version_id,)).fetchone()
    try:
        return json.loads(row['settings_json']) if row else {}
    except (ValueError, TypeError):
        return {}


def export_folder(dataset_hash: str, version_id: str) -> Path:
    """Deterministic scoped folder, never based on untrusted workbook filenames."""
    if not re.fullmatch(r'[0-9a-f]{64}', str(dataset_hash)):
        raise ValueError('Invalid workbook fingerprint')
    if not re.fullmatch(r'[0-9a-f]{16}', str(version_id)):
        raise ValueError('Invalid archived timetable version')
    return db_path().parent / 'saved_timetables' / dataset_hash[:16] / version_id


def _write_atomic(destination: Path, contents: bytes):
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, tempname = tempfile.mkstemp(prefix='.timora-', dir=destination.parent)
    try:
        with os.fdopen(handle, 'wb') as writer:
            writer.write(contents)
        os.replace(tempname, destination)
    finally:
        if os.path.exists(tempname):
            os.unlink(tempname)


def _context_settings(result: dict, kind: str, workbook_name: str, university: str) -> dict:
    return {
        'kind': kind,
        'workbook_name': workbook_name,
        'university': university,
        'include_mentor': bool(result.get('include_mentor', True)),
        'sim_count': int(result.get('sim_count', 0) or 0),
        'fairness_profile': str(result.get('fairness_profile', 'balanced')),
        'fairness_priority': str(result.get('fairness_priority', 'standard')),
        'highlighted_session_ids': list(result.get('highlighted_session_ids', []) or []),
        'fairness_ai_recommended': bool(result.get('fairness_ai_recommended', False)),
        'source_variant': str(result.get('source_variant', '')),
    }


def _source_files_for_version(result: dict, option: dict) -> dict[str, bytes]:
    prefix = option.get('prefix', '')
    saved = {}
    for key, value in result.get('files', {}).items():
        if prefix:
            if not key.startswith(prefix):
                continue
            name = key[len(prefix):]
        else:
            if key.startswith('baseline_'):
                continue
            name = key
        if name in _SAVED_FILE_NAMES and isinstance(value, bytes):
            saved[name] = value
    saved['timetable_all.csv'] = option['csv']
    if result.get('logs') and not prefix:
        saved['solver_log.txt'] = str(result['logs']).encode('utf-8', errors='replace')
    return saved


def _save_files(dataset_hash: str, version_id: str, files: dict[str, bytes]):
    folder = export_folder(dataset_hash, version_id)
    for name, data in files.items():
        if name in _SAVED_FILE_NAMES or name in PDF_NAMES:
            _write_atomic(folder / name, data)


def _pdfs(workbook: bytes, csv_blob: bytes, settings: dict) -> dict[str, bytes]:
    from timetable_pdf_export import build_pdf_exports
    return build_pdf_exports(csv_blob, workbook,
        university_name=settings.get('university') or 'University not specified',
        dataset_name=settings.get('workbook_name') or 'university.xlsx',
        version_name={'STANDARD': 'Standard CP-SAT', 'FAIRNESS': 'Fairness optimized',
                      'COORDINATOR_APPROVED': 'Coordinator-approved updated timetable',
                      'APPROVED': 'Coordinator-approved updated timetable'}.get(
            settings.get('kind'), settings.get('kind') or 'Saved timetable'),
        highlighted_session_ids=settings.get('highlighted_session_ids', ()))


def archive_result(workbook: bytes, result: dict) -> list[tuple[str, str]]:
    """Automatically archive all *independently validated* published variants.

    Does not save failures, unknown/infeasible results, or a fake Fairness version
    when the fairness solver fell back to Standard.
    """
    if result.get('status_code') != 0:
        return []
    options = available_versions(result)
    if not options:
        return []
    filename = result.get('input_file') or 'university.xlsx'
    university = result.get('university_name') or 'University not specified'
    digest = data_digest(workbook)
    archived = []
    for option in options:
        if option['kind'] not in ('STANDARD', 'FAIRNESS', 'APPROVED'):
            continue
        # Approval already has a durable version; never duplicate or downgrade it.
        version_id = (result.get('approved_version') if option['kind'] == 'APPROVED'
                      else None)
        settings = _context_settings(result, option['kind'], filename, university)
        if version_id:
            item = get_version(digest, version_id)
            if not item or schedule_hash(option['csv']) != item['schedule_hash']:
                raise ValueError('Approved version is not present in the audit log')
            settings['kind'] = 'COORDINATOR_APPROVED'
        else:
            checked = validate_bytes(workbook, option['csv'], settings['include_mentor'],
                                     settings['sim_count'])
            if checked.get('status') not in PASS:
                continue
        # A PDF export failing must not erase a verified source timetable.
        save_project(workbook, filename, university)
        if not version_id:
            summary = _source_files_for_version(result, option).get('quality_summary.json')
            try:
                metrics = json.loads(summary.decode('utf-8')).get('metrics', {}) if summary else {}
            except (ValueError, UnicodeError, TypeError):
                metrics = {}
            version_id = add_version(digest, option['csv'], option['kind'], metrics)
        _update_version_info(digest, version_id, settings)
        source_files = _source_files_for_version(result, option)
        _save_files(digest, version_id, source_files)
        # PDFs are kept on disk, in separate files for sections, faculty, rooms.
        folder = export_folder(digest, version_id)
        if not all((folder / name).exists() for name in PDF_NAMES):
            try:
                _save_files(digest, version_id, _pdfs(workbook, option['csv'], settings))
            except (ImportError, OSError, ValueError, RuntimeError) as exc:
                _write_atomic(folder / 'pdf_export_warning.txt', str(exc).encode('utf-8'))
        archived.append((option['kind'], version_id))
    return archived


def _moved_ids(dataset_hash: str, record: dict, metrics: dict) -> list[str]:
    stored = metrics.get('highlighted_session_ids') or []
    if stored:
        return list(dict.fromkeys(str(x) for x in stored))
    parent_id = record.get('parent_version_id')
    if not parent_id:
        return []
    parent = get_version(dataset_hash, parent_id)
    if not parent:
        return []
    before = {}
    after = {}
    for row in csv_rows(parent['csv_blob']):
        before.setdefault(str(row.get('session_id', '')), set()).add(
            (row.get('day'), row.get('period'), row.get('room_id')))
    for row in csv_rows(record['csv_blob']):
        after.setdefault(str(row.get('session_id', '')), set()).add(
            (row.get('day'), row.get('period'), row.get('room_id')))
    return sorted(sid for sid, positions in after.items() if sid and before.get(sid) != positions)


def _stored_result(dataset_hash: str, record: dict, project: dict) -> dict:
    from timetable_workflows_ui import rebuild_verified_artifacts
    from northcap_cp_sat_solver import excel_records
    kind = record['variant']
    settings = version_info(record['version_id'])
    settings.setdefault('kind', kind)
    settings.setdefault('university', project['university'])
    settings.setdefault('workbook_name', project['filename'])
    settings.setdefault('include_mentor', True)
    settings.setdefault('sim_count', 0)
    checked = validate_bytes(project['workbook_blob'], record['csv_blob'],
                             settings['include_mentor'], settings['sim_count'])
    if checked.get('status') not in PASS:
        raise ValueError('Saved timetable did not pass independent validation after reopening; not published')
    metrics = json.loads(record.get('metrics_json') or '{}')
    highlights = _moved_ids(dataset_hash, record, metrics)
    # Rebuild all views from the same archived CSV. This preserves consistency
    # even if an old HTML report was missing or the app was upgraded.
    rows = csv_rows(record['csv_blob'])
    data = excel_records(io.BytesIO(project['workbook_blob']))
    files, quality = rebuild_verified_artifacts(
        data, rows, include_mentoring=settings['include_mentor'],
        extra_labs=settings['sim_count'], highlighted_session_ids=highlights,
        quality_mode='restored_archive', dataset_name=settings['workbook_name'],
        university_name=settings['university'],
        timetable_type={'STANDARD':'Standard CP-SAT', 'FAIRNESS':'Fairness optimized',
                        'COORDINATOR_APPROVED':'Updated Timetable'}.get(kind,kind.title()))
    folder = export_folder(dataset_hash, record['version_id'])
    if folder.is_dir():
        for name in ('policy_compliance.json', 'policy_compliance.txt', 'solver_log.txt'):
            file = folder / name
            if file.is_file():
                files[name] = file.read_bytes()
    settings['highlighted_session_ids'] = highlights
    _update_version_info(dataset_hash, record['version_id'], settings)
    return {
        'files': files,
        'status_code': 0,
        'independent_status': checked['status'],
        'baseline_independent_status': 'NOT_RUN',
        'input_file': settings['workbook_name'],
        'university_name': settings['university'],
        'include_mentor': settings['include_mentor'],
        'sim_count': settings['sim_count'],
        'fairness_enabled': kind == 'FAIRNESS',
        'fairness_requested': kind == 'FAIRNESS',
        'fairness_profile': settings.get('fairness_profile','balanced'),
        'fairness_priority': settings.get('fairness_priority','standard'),
        'fairness_ai_recommended': settings.get('fairness_ai_recommended', False),
        'fell_back_to_standard': False,
        'rescheduled': kind == 'COORDINATOR_APPROVED',
        'approved_version': record['version_id'] if kind == 'COORDINATOR_APPROVED' else None,
        'highlighted_session_ids': highlights,
        'restored_archived_version': record['version_id'],
        'source_variant': settings.get('source_variant',''),
        'comparison_requested': False,
        'comparison': None,
        'comparison_guards': None,
        'logs': (files.get('solver_log.txt',b'').decode('utf-8',errors='replace') +
                 '\nREOPENED ARCHIVED TIMETABLE: independently revalidated CSV; no fresh CP-SAT solve.\n'),
    }


def restore_version(dataset_hash: str, version_id: str) -> tuple[dict, dict]:
    """Restore original Excel + validated saved timetable after a new session."""
    project = saved_project(dataset_hash)
    record = get_version(dataset_hash, version_id)
    if not project or not record:
        raise ValueError('Saved project or version was not found on this installation')
    if data_digest(project['workbook_blob']) != dataset_hash:
        raise ValueError('Stored workbook fingerprint does not match its record')
    return project, _stored_result(dataset_hash, record, project)


def saved_exports(dataset_hash: str, version_id: str) -> dict[str, bytes]:
    """Provide the same version's 3 PDFs after restarts; generate missing PDFs."""
    project = saved_project(dataset_hash)
    record = get_version(dataset_hash, version_id)
    if not project or not record:
        raise ValueError('Unknown workbook or archived timetable version')
    settings = version_info(version_id)
    settings.setdefault('kind', record['variant'])
    settings.setdefault('workbook_name', project['filename'])
    settings.setdefault('university', project['university'])
    settings.setdefault('include_mentor', True)
    settings.setdefault('sim_count', 0)
    folder = export_folder(dataset_hash, version_id)
    # On older coordinator records, infer MOVED markers from the saved parent.
    try:
        metrics = json.loads(record.get('metrics_json') or '{}')
    except (TypeError, ValueError):
        metrics = {}
    highlights = _moved_ids(dataset_hash, record, metrics)
    if tuple(settings.get('highlighted_session_ids', [])) != tuple(highlights):
        settings['highlighted_session_ids'] = highlights
        _update_version_info(dataset_hash, version_id, settings)
        for name in PDF_NAMES:
            (folder/name).unlink(missing_ok=True)
    if not all((folder/name).exists() for name in PDF_NAMES):
        status = validate_bytes(project['workbook_blob'], record['csv_blob'],
                                settings['include_mentor'], settings['sim_count'])
        if status.get('status') not in PASS:
            raise ValueError('Archived timetable failed independent validation; PDFs were not created')
        pdfs = _pdfs(project['workbook_blob'], record['csv_blob'], settings)
        _save_files(dataset_hash, version_id, pdfs)
    return {name:(folder/name).read_bytes() for name in PDF_NAMES}


def saved_bundle(dataset_hash: str, version_id: str) -> bytes:
    """Create one ZIP with separate section, faculty, room PDF and CSV views."""
    from zipfile import ZIP_DEFLATED, ZipFile
    pdfs = saved_exports(dataset_hash, version_id)
    record = get_version(dataset_hash, version_id)
    folder = export_folder(dataset_hash, version_id)
    import io
    buffer = io.BytesIO()
    with ZipFile(buffer, 'w', ZIP_DEFLATED) as bundle:
        for name, data in pdfs.items():
            bundle.writestr(name, data)
        for name in CSV_NAMES:
            if (folder/name).is_file():
                bundle.writestr(name, (folder/name).read_bytes())
        if not (folder/'timetable_all.csv').is_file():
            bundle.writestr('timetable_all.csv', record['csv_blob'])
    return buffer.getvalue()
