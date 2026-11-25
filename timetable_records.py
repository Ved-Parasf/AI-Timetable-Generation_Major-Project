"""SQLite audit log for schedule versions and coordinator demo requests.

No passwords/accounts are created. This is a *local demo workflow*, not secure
university authentication. Deployments must add identity checks and RBAC before
using these approval endpoints outside a controlled prototype.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from timetable_rescheduling import schedule_hash


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def data_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def db_path():
    custom = os.environ.get('SCHEDULE_STUDIO_DB_PATH')
    path = Path(custom) if custom else Path(__file__).with_name('data') / 'coordinator_workflows.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect():
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS versions (
        version_id TEXT PRIMARY KEY,
        dataset_hash TEXT NOT NULL,
        schedule_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        variant TEXT NOT NULL,
        parent_version_id TEXT,
        request_id INTEGER,
        metrics_json TEXT NOT NULL,
        csv_blob BLOB NOT NULL,
        UNIQUE(dataset_hash, schedule_hash)
    );
    CREATE INDEX IF NOT EXISTS versions_dataset ON versions(dataset_hash, created_at);
    CREATE TABLE IF NOT EXISTS requests (
        request_id INTEGER PRIMARY KEY AUTOINCREMENT,
        dataset_hash TEXT NOT NULL,
        base_schedule_hash TEXT NOT NULL,
        source_role TEXT NOT NULL CHECK(source_role IN ('Teacher','Student')),
        display_name TEXT NOT NULL,
        session_id TEXT NOT NULL,
        desired_slot TEXT NOT NULL,
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED')),
        coordinator_note TEXT NOT NULL DEFAULT '',
        selected_slot TEXT,
        approved_version_id TEXT,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    );
    CREATE INDEX IF NOT EXISTS requests_dataset ON requests(dataset_hash, status, created_at);
    ''')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def add_version(dataset_hash: str, csv_blob: bytes, variant: str, metrics: dict | None = None,
                parent_version_id=None, request_id=None):
    digest = schedule_hash(csv_blob)
    vid = uuid.uuid4().hex[:16]
    with connect() as db:
        row = db.execute('SELECT version_id FROM versions WHERE dataset_hash=? AND schedule_hash=?',
                         (dataset_hash, digest)).fetchone()
        if row:
            return row['version_id']
        db.execute('''INSERT INTO versions(version_id,dataset_hash,schedule_hash,created_at,variant,
                      parent_version_id,request_id,metrics_json,csv_blob)
                      VALUES(?,?,?,?,?,?,?,?,?)''',
                   (vid, dataset_hash, digest, utcnow(), variant, parent_version_id,
                    request_id, json.dumps(metrics or {}, sort_keys=True), csv_blob))
    return vid


def list_versions(dataset_hash: str):
    with connect() as db:
        return [dict(x) for x in db.execute('''SELECT version_id, schedule_hash, created_at,variant,
                parent_version_id,request_id,metrics_json FROM versions
                WHERE dataset_hash=? ORDER BY created_at DESC, rowid DESC''', (dataset_hash,))]


def get_version(dataset_hash: str, version_id: str):
    with connect() as db:
        row = db.execute('''SELECT * FROM versions WHERE dataset_hash=? AND version_id=?''',
                         (dataset_hash, version_id)).fetchone()
        return dict(row) if row else None


def submit_request(dataset_hash: str, baseline_csv: bytes, role: str, display_name: str,
                   session_id: str, desired_slot: str, reason: str):
    if role not in ('Teacher', 'Student'):
        raise ValueError('Only Teacher or Student may submit a preference request')
    if not all([display_name.strip(), session_id.strip(), desired_slot.strip(), reason.strip()]):
        raise ValueError('Name, teaching session, desired slot and reason are required')
    if max(map(len, (display_name,session_id,desired_slot,reason))) > 1000:
        raise ValueError('Request text is too long')
    digest = schedule_hash(baseline_csv)
    with connect() as db:
        cursor = db.execute('''INSERT INTO requests(dataset_hash,base_schedule_hash,source_role,
                  display_name,session_id,desired_slot,reason,status,created_at)
                  VALUES(?,?,?,?,?,?,?,'PENDING',?)''',
                  (dataset_hash,digest,role,display_name.strip(),session_id.strip(),
                   desired_slot.strip(),reason.strip(),utcnow()))
        return cursor.lastrowid


def list_requests(dataset_hash: str, status=None):
    with connect() as db:
        q = 'SELECT * FROM requests WHERE dataset_hash=?'
        values = [dataset_hash]
        if status:
            q += ' AND status=?'; values.append(status)
        q += ' ORDER BY created_at DESC, request_id DESC'
        return [dict(x) for x in db.execute(q,values)]


def get_request(dataset_hash: str, request_id: int):
    with connect() as db:
        row = db.execute('SELECT * FROM requests WHERE dataset_hash=? AND request_id=?',
                         (dataset_hash, request_id)).fetchone()
        return dict(row) if row else None


def reject_request(dataset_hash: str, request_id: int, note: str):
    if not note.strip():
        raise ValueError('Enter a short reason for rejecting the request')
    with connect() as db:
        result = db.execute('''UPDATE requests SET status='REJECTED',coordinator_note=?,resolved_at=?
                WHERE dataset_hash=? AND request_id=? AND status='PENDING' ''',
                (note.strip(),utcnow(),dataset_hash,request_id))
        if result.rowcount != 1:
            raise ValueError('Request is no longer pending or belongs to a different workbook')


def approve_request(dataset_hash: str, request_id: int, expected_base_hash: str,
                    approved_csv: bytes, selected_slot: str, metrics: dict | None = None,
                    note: str = ''):
    """Commit an already independently validated schedule and approval together.

    Caller must independently validate *approved_csv*. This function protects
    against stale pending requests and duplicate approval transactions.
    """
    vid = uuid.uuid4().hex[:16]
    approved_hash = schedule_hash(approved_csv)
    with connect() as db:
        pending = db.execute('SELECT * FROM requests WHERE request_id=? AND dataset_hash=?',
                             (request_id, dataset_hash)).fetchone()
        if not pending or pending['status'] != 'PENDING':
            raise ValueError('Request is not pending')
        if pending['base_schedule_hash'] != expected_base_hash:
            raise ValueError('Request was created against a different timetable; refresh or resubmit')
        if expected_base_hash == approved_hash:
            raise ValueError('Proposed timetable must actually move the selected session')
        old = db.execute('SELECT version_id FROM versions WHERE dataset_hash=? AND schedule_hash=?',
                         (dataset_hash, expected_base_hash)).fetchone()
        existing = db.execute('SELECT version_id FROM versions WHERE dataset_hash=? AND schedule_hash=?',
                              (dataset_hash, approved_hash)).fetchone()
        if existing:
            vid = existing['version_id']
        else:
            db.execute('''INSERT INTO versions(version_id,dataset_hash,schedule_hash,created_at,
                variant,parent_version_id,request_id,metrics_json,csv_blob)
                VALUES(?,?,?,?,'COORDINATOR_APPROVED',?,?,?,?)''',
                (vid,dataset_hash,approved_hash,utcnow(),old['version_id'] if old else None,
                 request_id,json.dumps(metrics or {},sort_keys=True),approved_csv))
        cursor = db.execute('''UPDATE requests SET status='APPROVED',coordinator_note=?,
                selected_slot=?,approved_version_id=?,resolved_at=?
                WHERE request_id=? AND status='PENDING' AND base_schedule_hash=?''',
                (note.strip(),selected_slot,vid,utcnow(),request_id,expected_base_hash))
        if cursor.rowcount != 1:
            raise ValueError('Concurrent approval modified this request; transaction rolled back')
    return vid


def unsave_manual_version(dataset_hash: str, version_id: str, expected_schedule_hash: str | None = None) -> bool:
    """Remove only an explicitly saved Standard/Fairness entry from local history.

    Approval records, parent audit snapshots and any timetable referenced by a
    class-move request are immutable. No generated CSV on disk is deleted.
    The entire decision and deletion are atomic in one SQLite transaction.
    """
    with connect() as db:
        row = db.execute('''SELECT version_id, variant, schedule_hash FROM versions
            WHERE dataset_hash=? AND version_id=?''', (dataset_hash, version_id)).fetchone()
        if row is None:
            return False
        if row['variant'] not in ('STANDARD', 'FAIRNESS'):
            raise ValueError('This timetable is part of a revision/audit record and cannot be unsaved.')
        if expected_schedule_hash and row['schedule_hash'] != expected_schedule_hash:
            raise ValueError('The selected timetable has changed. Refresh the page and try again.')
        if db.execute('''SELECT 1 FROM versions WHERE dataset_hash=?
                AND parent_version_id=? LIMIT 1''', (dataset_hash, version_id)).fetchone():
            raise ValueError('This timetable is referenced by an approved revision and must be retained.')
        if db.execute('''SELECT 1 FROM requests WHERE dataset_hash=? AND
                base_schedule_hash=? LIMIT 1''', (dataset_hash, row['schedule_hash'])).fetchone():
            raise ValueError('This timetable has class-move requests and must be retained as an audit source.')
        if db.execute('''SELECT 1 FROM requests WHERE dataset_hash=? AND
                approved_version_id=? LIMIT 1''', (dataset_hash, version_id)).fetchone():
            raise ValueError('This timetable is linked to an approval and cannot be removed.')
        deleted = db.execute('DELETE FROM versions WHERE dataset_hash=? AND version_id=?',
                             (dataset_hash, version_id))
        return deleted.rowcount == 1


def commit_chat_coordinator_move(workbook_blob: bytes, baseline_csv: bytes, revised_csv: bytes,
                                 *, session_id: str, destination_slot_id: str,
                                 source_variant: str = 'STANDARD', note: str = '',
                                 metrics: dict | None = None, include_mentoring: bool = True,
                                 extra_simulated_labs: int = 0) -> str:
    """Atomically archive an explicitly confirmed, independently validated chat move.

    This represents a *local demo coordinator* pressing the UI confirmation,
    NOT an authenticated university account. No original Excel/CSV is overwritten.
    Both the immutable parent and revised CSV are archived in a single transaction.
    """
    from timetable_rescheduling import csv_rows
    from timetable_validator import validate_bytes
    from northcap_cp_sat_solver import excel_records
    import io

    if not session_id or not destination_slot_id:
        raise ValueError('A specific session and destination slot are required')
    if baseline_csv == revised_csv or schedule_hash(baseline_csv) == schedule_hash(revised_csv):
        raise ValueError('No timetable change was supplied')

    # Check the original and replacement with the separate independent validator.
    for label, blob in [('Source', baseline_csv), ('Revised', revised_csv)]:
        check = validate_bytes(workbook_blob, blob, include_mentoring, extra_simulated_labs)
        if check.get('status') not in ('PASS', 'PASS_WITH_LIMITATIONS'):
            raise ValueError(f'{label} timetable failed independent validation; change not saved')

    before, after = csv_rows(baseline_csv), csv_rows(revised_csv)
    source_rows = [r for r in before if r.get('session_id') == session_id]
    target_rows = [r for r in after if r.get('session_id') == session_id]
    if not source_rows or len(source_rows) != len(target_rows):
        raise ValueError('The selected teaching session was not preserved')
    def stable_others(rows):
        return sorted(tuple(sorted(row.items())) for row in rows if row.get('session_id') != session_id)
    if stable_others(before) != stable_others(after):
        raise ValueError('Only the selected session may move in this workflow')
    data = excel_records(io.BytesIO(workbook_blob))
    desired = next((slot for slot in data.get('Time_Slots', [])
                    if str(slot.get('slot_id', '')).strip() == destination_slot_id), None)
    if desired is None:
        raise ValueError('Destination slot is not defined in the workbook')
    first = sorted(target_rows, key=lambda r: int(float(r.get('period') or 0)))[0]
    if (str(first.get('day', '')).upper() != str(desired['day']).upper()
            or int(float(first['period'])) != int(float(desired['period']))):
        raise ValueError('Validated move does not start at the selected destination slot')

    metrics = dict(metrics or {}, include_mentor=include_mentoring,
                   sim_count=extra_simulated_labs)
    workbook_hash = data_digest(workbook_blob)
    before_hash, after_hash = schedule_hash(baseline_csv), schedule_hash(revised_csv)
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS coordinator_chat_edits (
          edit_id INTEGER PRIMARY KEY AUTOINCREMENT,
          dataset_hash TEXT NOT NULL,
          before_version_id TEXT NOT NULL,
          after_version_id TEXT NOT NULL,
          session_id TEXT NOT NULL,
          destination_slot TEXT NOT NULL,
          note TEXT NOT NULL,
          approved_by TEXT NOT NULL,
          created_at TEXT NOT NULL
        )''')
        original = db.execute('SELECT version_id FROM versions WHERE dataset_hash=? AND schedule_hash=?',
                              (workbook_hash, before_hash)).fetchone()
        if original:
            previous_id = original['version_id']
        else:
            previous_id = uuid.uuid4().hex[:16]
            db.execute('''INSERT INTO versions (version_id,dataset_hash,schedule_hash,created_at,
                variant,parent_version_id,request_id,metrics_json,csv_blob)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (previous_id,workbook_hash,before_hash,utcnow(),'PRE_CHAT_REVISION',None,None,
                 '{}',baseline_csv))
        existing = db.execute('SELECT version_id FROM versions WHERE dataset_hash=? AND schedule_hash=?',
                              (workbook_hash, after_hash)).fetchone()
        if existing:
            raise ValueError('This revised timetable is already stored; reload its saved version before continuing')
        new_id = uuid.uuid4().hex[:16]
        db.execute('''INSERT INTO versions (version_id,dataset_hash,schedule_hash,created_at,
            variant,parent_version_id,request_id,metrics_json,csv_blob)
            VALUES(?,?,?,?,?,?,?,?,?)''',
            (new_id,workbook_hash,after_hash,utcnow(),'COORDINATOR_APPROVED',previous_id,None,
             json.dumps(metrics or {},sort_keys=True),revised_csv))
        db.execute('''INSERT INTO coordinator_chat_edits(dataset_hash,before_version_id,
            after_version_id,session_id,destination_slot,note,approved_by,created_at)
            VALUES(?,?,?,?,?,?,?,?)''',
            (workbook_hash,previous_id,new_id,session_id,destination_slot_id,note[:1000],
             'DEMO_COORDINATOR',utcnow()))
    return new_id


def list_chat_edits(workbook_blob: bytes):
    """Return persisted direct-chat approval history for this dataset."""
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS coordinator_chat_edits (
          edit_id INTEGER PRIMARY KEY AUTOINCREMENT, dataset_hash TEXT NOT NULL,
          before_version_id TEXT NOT NULL, after_version_id TEXT NOT NULL,
          session_id TEXT NOT NULL, destination_slot TEXT NOT NULL,
          note TEXT NOT NULL, approved_by TEXT NOT NULL, created_at TEXT NOT NULL)''')
        return [dict(row) for row in db.execute('''SELECT * FROM coordinator_chat_edits
            WHERE dataset_hash=? ORDER BY edit_id DESC''', (data_digest(workbook_blob),))]

