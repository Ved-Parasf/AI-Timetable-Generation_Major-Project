"""Coordinator workflow UI, kept separate from V10.1 timetable components.

This module accepts a Streamlit object as a parameter so non-UI modules and
audit/validation tests do not depend on Streamlit being installed in CI.
"""
from __future__ import annotations

import io
import json
import tempfile
from collections import defaultdict
from pathlib import Path

from northcap_cp_sat_solver import excel_records, clean, write_csv
from timetable_rescheduling import (
    csv_bytes, csv_rows, timetable_sessions, propose_moves, schedule_hash,
)
from timetable_records import (
    data_digest, add_version, approve_request, get_request, get_version,
    list_requests, list_versions, reject_request, submit_request,
)
from timetable_validator import validate, report_as_text
from timetable_fairness import quality_report, save_quality_files
from timetable_policies import load_policies
from timetable_report import render_timetable_html

PASS = ('PASS', 'PASS_WITH_LIMITATIONS')


def display_session(rows):
    first = rows[0]
    periods = sorted({int(float(r['period'])) for r in rows})
    start = min(periods)
    end = max(periods)
    return (f"{clean(first['course_id'])} / {clean(first['group_id'])} | "
            f"{clean(first['faculty_name'])} | {clean(first['day'])} P{start}"
            + (f"–P{end}" if end != start else '')
            + f" | {clean(first['kind'])}")


def slot_label(slot):
    return (f"{clean(slot['day'])} · Period {int(float(slot['period']))} "
            f"({clean(slot['start_time'])}–{clean(slot['end_time'])})")


def rebuild_verified_artifacts(data, new_rows, *, include_mentoring=True, extra_labs=0,
                               quality_mode='coordinator_approved', highlighted_session_ids=(),
                               dataset_name=None, university_name=None, timetable_type='Updated Timetable'):
    """Recreate all affected outputs after approval; fail closed on validation."""
    checked = validate(data, new_rows, include_mentoring, extra_labs)
    if checked['status'] not in PASS:
        raise ValueError('Independent validation rejected updated timetable: '
                         + '; '.join(x['message'] for x in checked['errors'][:4]))
    policies = load_policies(data)
    groups = {clean(r['group_id']):r for r in data['Student_Groups']}
    quality = quality_report(new_rows, groups, mode=quality_mode, policies=policies)
    html = render_timetable_html(
        new_rows, groups, data['Time_Slots'],
        reserved_elective_slot_ids=policies.reserved_slot_ids,
        elective_min_semester=policies.elective_min_sem,
        elective_courses=policies.window_allowed_course_codes,
        elective_window_mode=policies.elective_window_mode,
        fixed_non_teaching_blocks=policies.fixed_non_teaching_blocks,
        highlighted_session_ids=highlighted_session_ids,
        dataset_name=dataset_name, university_name=university_name,
        timetable_type=timetable_type, rooms=data['Rooms'],
    )
    result = {
        'timetable_all.csv': csv_bytes(new_rows),
        'timetable_faculty.csv': csv_bytes(sorted(new_rows,
            key=lambda r:(clean(r['faculty_name']),policies.days.index(clean(r['day']).upper()),int(float(r['period']))))),
        'timetable_rooms.csv': csv_bytes(sorted(new_rows,
            key=lambda r:(clean(r['room_id']),policies.days.index(clean(r['day']).upper()),int(float(r['period']))))),
        'timetable_groups.csv': csv_bytes(sorted(new_rows,
            key=lambda r:(clean(r['group_id']),policies.days.index(clean(r['day']).upper()),int(float(r['period']))))),
        'timetable_sections.html': html.encode('utf-8'),
        'independent_validation.json': json.dumps(checked, indent=2).encode('utf-8'),
        'independent_validation.txt': report_as_text(checked).encode('utf-8'),
        'validation_report.txt': report_as_text(checked).encode('utf-8'),
    }
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory)
        save_quality_files(output, quality, write_csv)
        for file in output.glob('*'):
            if file.is_file():
                result[file.name] = file.read_bytes()
    return result, quality


def save_generation_versions(blob, result):
    """Non-destructive history of validated standard/fairness outputs."""
    digest = data_digest(blob)
    names = [
        ('baseline_', 'STANDARD'),
        ('', 'FAIRNESS' if result.get('fairness_enabled') else 'STANDARD'),
    ]
    entries = []
    for prefix, label in names:
        csv_content = result.get('files', {}).get(prefix + 'timetable_all.csv')
        checked = (result.get('baseline_independent_status') if prefix
                   else result.get('independent_status'))
        if not csv_content or checked not in PASS:
            continue
        report = result['files'].get(prefix + 'quality_summary.json')
        try:
            q = json.loads(report.decode('utf-8')).get('metrics', {}) if report else {}
            version = add_version(digest, csv_content, label, q)
            entries.append(version)
        except Exception as exc:
            result['history_warning'] = f'Could not save timetable history: {type(exc).__name__}: {exc}'
    return entries


def _request_form(st, role, data, rows, csv_content, digest):
    sessions = timetable_sessions(rows)
    schedule_ids = sorted(sessions)
    if role == 'Teacher':
        staff = sorted({(clean(r['faculty_id']), clean(r['faculty_name'])) for r in rows})
        ids = [fid for fid, _ in staff]
        selection = st.selectbox('Choose teacher (demo identity)', ids,
                                 format_func=lambda fid: next((f'{name} ({fid})' for a,name in staff if a==fid),fid),
                                 key='workflow_teacher')
        eligible = [sid for sid in schedule_ids if clean(sessions[sid][0]['faculty_id']) == selection]
        display_name = next((name for fid,name in staff if fid == selection), selection)
        st.caption('Teacher identity is selected for a local demonstration. This is not a logged-in teacher account.')
    else:
        groups = sorted({clean(r['group_id']) for r in rows})
        selection = st.selectbox('Choose student group/section (demo)', groups, key='workflow_student_group')
        eligible = [sid for sid in schedule_ids if clean(sessions[sid][0]['group_id']) == selection]
        display_name = 'Group feedback: ' + selection
        st.caption('This is a group-level prototype request, not authenticated individual student voting.')
    if not eligible:
        st.info('No sessions are available for the selected group/person.')
        return
    with st.form('preference_request_form', clear_on_submit=False):
        sid = st.selectbox('Class to move', eligible, format_func=lambda x: f'{x}: {display_session(sessions[x])}')
        options = sorted(data['Time_Slots'], key=lambda x:(clean(x['day']),int(float(x['period']))))
        preferred_id = st.selectbox('Preferred new start slot', [clean(s['slot_id']) for s in options],
            format_func=lambda x: next((slot_label(s) for s in options if clean(s['slot_id'])==x),x))
        reason = st.text_area('Reason for requesting the move', max_chars=600,
                              placeholder='e.g. Prefer fewer teaching gaps on Wednesday')
        if st.form_submit_button('Submit preference request', type='primary'):
            if not reason.strip():
                st.error('Please enter a reason for the request.')
            else:
                try:
                    reqid = submit_request(digest, csv_content, role, display_name, sid, preferred_id, reason)
                    st.success(f'Request #{reqid} saved as PENDING. The timetable has not been changed.')
                except Exception as exc:
                    st.error('Request not saved: '+str(exc))


def _review_requests(st, data, rows, csv_content, digest, result):
    pending = list_requests(digest, 'PENDING')
    if not pending:
        st.info('No pending change requests. Use Teacher or Student role to submit a demonstration request.')
        return
    requests = {x['request_id']:x for x in pending}
    req_id = st.selectbox('Pending request', sorted(requests,reverse=True),
                          format_func=lambda i: f"#{i} · {requests[i]['source_role']} · {requests[i]['session_id']} → {requests[i]['desired_slot']}",
                          key='workflow_review_id')
    record = requests[req_id]
    base_hash = schedule_hash(csv_content)
    st.write(f"**From:** {record['display_name']} ({record['source_role']})")
    st.write(f"**Reason:** {record['reason']}")
    if record['base_schedule_hash'] != base_hash:
        st.error('This request was submitted against a different timetable version. '
                 'It cannot be applied to the current timetable. Reject and resubmit it against the current version.')
    otherwise = record['base_schedule_hash'] == base_hash
    left, right = st.columns([1, 1])
    with left:
        preview_clicked = st.button('Check conflicts and propose moves', key='workflow_preview',
                                    disabled=not otherwise, use_container_width=True, type='primary')
    if preview_clicked:
        with st.spinner('Checking candidate slots against the full timetable and university policies...'):
            proposal = propose_moves(data, rows, record['session_id'],
                requested_slot_id=record['desired_slot'],
                include_mentoring=result.get('include_mentor',True),
                extra_simulated_labs=result.get('sim_count',0), maximum_options=12)
        st.session_state['coordinator_preview'] = {
            'request_id':req_id, 'base_hash':base_hash, 'proposal':proposal}
        reasons = proposal.get('requested_reasons', [])
        st.session_state['coordinator_last_diagnosis'] = (
            f"Session {record['session_id']}, requested {record['desired_slot']}; "
            f"status {proposal['status']}. Requested-slot findings: " +
            ('; '.join(reasons) if reasons else 'No requested-slot conflict shown') + '. ' +
            'Other obstacles: ' + '; '.join(f'{what} ({n} slot(s))' for what,n in proposal.get('other_reasons', [])))
    state = st.session_state.get('coordinator_preview')
    active = (state and state['request_id'] == req_id and state['base_hash'] == base_hash)
    if active:
        proposal = state['proposal']
        st.write(f"**Original:** {proposal.get('current_slot','—')} · Room {proposal.get('current_room','—')}")
        if proposal.get('requested_reasons'):
            st.warning('Requested slot cannot be used as entered: ' + '; '.join(proposal['requested_reasons']))
        for why, count in proposal.get('other_reasons', [])[:4]:
            st.caption(f'Other examined slots: {why} ({count})')
        options = proposal.get('options', [])
        if options:
            st.success(f"{proposal['examined_valid_options']} independently verified alternative start slots found. "
                       f"Recommendation selected using {proposal['selection_engine']}.")
            st.dataframe([{
                'Day and periods':o['slot'], 'Room':o['room_id'],
                'Requested': 'Yes' if o['requested'] else 'No',
                'Faculty gap change':o['delta']['faculty_gaps'],
                'Student gap change':o['delta']['section_gaps'],
                'Late periods change':o['delta']['late_periods'],
                'Soft-avoid change':o['delta']['soft_avoid_periods'],
            } for o in options],use_container_width=True,hide_index=True)
            selected = st.selectbox('Coordinator proposed placement', list(range(len(options))),
                index=proposal['recommended_index'] or 0,
                format_func=lambda i: f"{options[i]['slot']} · {options[i]['room_id']}"
                     + (' [requested]' if options[i]['requested'] else ''),
                key=f'workflow_choice_{req_id}')
            choice = options[selected]
            if choice['delta']['soft_avoid_periods'] > 0 or choice['delta']['late_periods'] > 0:
                st.warning('This proposed move increases late periods or occupied soft-avoid slots. '
                           'These are soft preferences, not hard-rule violations; inspect the impact before approval.')
            st.info('Only the selected teaching session moves. All other sessions stay in their original times and rooms. '
                    'The complete revised timetable has passed independent validation.')
            confirmed = st.checkbox('I reviewed the proposed move and its quality impact (demo coordinator approval)',
                                     key=f'workflow_confirmation_{req_id}')
            if st.button('Approve and publish revised timetable', type='primary',
                         disabled=not confirmed or not otherwise, key='workflow_approve'):
                try:
                    # Re-check the specific in-memory preview; prevent stale approvals.
                    latest = st.session_state.solver_result
                    source_key = result.get('_active_source_csv_key', 'timetable_all.csv')
                    latest_base = latest['files'].get(source_key)
                    if not latest_base or schedule_hash(latest_base) != base_hash:
                        raise ValueError('Selected timetable changed after preview; generate a new preview')
                    moved_sessions = sorted(set(latest.get('highlighted_session_ids', []))
                                            | {record['session_id']})
                    artifacts, quality = rebuild_verified_artifacts(data, choice['rows'],
                        include_mentoring=result.get('include_mentor',True),
                        extra_labs=result.get('sim_count',0),
                        highlighted_session_ids=moved_sessions,
                        dataset_name=result.get('input_file'),
                        university_name=result.get('university_name'))
                    new_blob = artifacts['timetable_all.csv']
                    # Approval is an explicit publishing action: record the
                    # untouched source schedule before committing its revision.
                    # This is an audit snapshot, not an automatic generation save.
                    source_metrics = {}
                    summary = result['files'].get('quality_summary.json')
                    if summary:
                        try:
                            source_metrics = json.loads(summary.decode('utf-8')).get('metrics', {})
                        except (ValueError, UnicodeError):
                            pass
                    add_version(digest, csv_content, result.get('_active_kind', 'PRE_REVISION'),
                                source_metrics)
                    version_id = approve_request(digest,req_id,base_hash,new_blob,
                        choice['slot_id'], dict(quality['metrics'], highlighted_session_ids=moved_sessions,
                                                include_mentor=result.get('include_mentor', True),
                                                sim_count=result.get('sim_count', 0)),
                        note='Demo coordinator approved the independently checked move')
                    updated = dict(latest)
                    files = {k: v for k, v in latest['files'].items()
                             if not k.startswith('baseline_')}
                    files.update(artifacts)
                    files.pop('quality_comparison.json',None)
                    files.pop('quality_comparison.txt',None)
                    updated['files'] = files
                    updated['fairness_enabled'] = False
                    updated['fairness_requested'] = False
                    updated['comparison_requested'] = False
                    updated['comparison'] = None
                    updated['comparison_guards'] = None
                    updated['independent_status'] = json.loads(artifacts['independent_validation.json'].decode('utf-8'))['status']
                    updated['baseline_independent_status'] = 'NOT_RUN'
                    updated['highlighted_session_ids'] = moved_sessions
                    updated['rescheduled'] = True
                    updated.pop('loaded_saved_version', None)
                    updated['approved_version'] = version_id
                    # Store the three view exports for this separate approved version.
                    try:
                        from timetable_persistence import save_workbook, read_workbook, ensure_version_files
                        save_workbook(st.session_state.workbook_bytes,
                                      result.get('input_file') or 'university.xlsx',
                                      result.get('university_name', ''))
                        ensure_version_files(read_workbook(digest), version_id)
                    except Exception as export_error:
                        st.warning('Approved change saved, but PDF preparation needs retry: ' + str(export_error))
                    updated['source_variant'] = result.get('_active_kind', 'STANDARD')
                    updated['logs'] = latest.get('logs','') + (
                        '\nCOORDINATOR-APPROVED REVISION: one session moved from the validated '
                        'timetable. Result independently revalidated. Original CP-SAT log above '
                        'describes the original generation, not this revision.\n')
                    st.session_state.solver_result = updated
                    st.session_state['assistant_last_published'] = (
                        f"Session {record['session_id']} approved at {choice['slot']}. "
                        f"Saved revision {version_id}.")
                    st.session_state.coordinator_preview = None
                    st.session_state.assistant_history = []
                    st.session_state['reset_timetable_variant_on_next_run'] = True
                    st.session_state.pop('chat_move_state', None)
                    st.success(f'Revision {version_id} approved, saved and published locally.')
                    st.rerun()
                except Exception as exc:
                    st.error('Approval blocked; published timetable unchanged: '+str(exc))
        else:
            st.error('No feasible one-session move was found. Nothing was changed. '
                     'The requested change may require moving other classes (outside this release).')
    with right:
        reject_reason = st.text_input('Coordinator rejection note',key=f'workflow_reject_note_{req_id}',
            placeholder='e.g. No feasible slot without moving another class')
        if st.button('Reject request', disabled=not bool(reject_reason.strip()),key='workflow_reject'):
            try:
                reject_request(digest, req_id, reject_reason)
                st.session_state['coordinator_preview'] = None
                st.success('Request rejected. No timetable changes made.')
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


def _history(st, data, digest, result):
    versions = list_versions(digest)
    if not versions:
        st.info('No recorded timetable versions yet. Generate a validated timetable first.')
        return
    st.dataframe([{'Version':v['version_id'], 'Type':v['variant'], 'Saved (UTC)':v['created_at'],
                   'Faculty gaps':json.loads(v['metrics_json']).get('teacher_gap_periods','—'),
                   'Late periods':json.loads(v['metrics_json']).get('late_session_periods','—')}
                  for v in versions[:50]],use_container_width=True,hide_index=True)
    chosen = st.selectbox('Download a saved timetable', [v['version_id'] for v in versions],
                         format_func=lambda vid: next(f"{v['variant']} — {v['created_at']} — {vid}" for v in versions if v['version_id']==vid),
                         key='workflow_history_choice')
    version = get_version(digest, chosen)
    st.download_button('Download saved timetable CSV',version['csv_blob'],
                       file_name=f'timetable_{chosen}.csv',mime='text/csv')
    st.caption('Original and approved versions are retained. Saved version downloads are not automatically published.')


def show_workflows(st, result, rows, workbook_blob):
    st.markdown('### AI Coordinator · Conflicts · Rescheduling')
    st.caption('Integrated with your existing timetable. Local prototype with demo roles: '
               'not secure teacher/student authentication or official university approval.')
    if result.get('independent_status') not in PASS:
        st.error('No validated baseline is available. Rescheduling is disabled.')
        return
    if result.get('history_warning'):
        st.warning(result['history_warning'])
    data = excel_records(io.BytesIO(workbook_blob))
    digest = data_digest(workbook_blob)
    csv_content = result['files']['timetable_all.csv']
    st.info('Active timetable for this change: **' + result.get('_active_label', 'Current timetable')
            + '**. Only this selected CSV is checked. A saved historical version is read-only in the chat.')
    role = st.radio('Choose demo workflow', ['Teacher','Student','Coordinator'],
                    horizontal=True,key='coordinator_role')
    if role in ('Teacher','Student'):
        st.markdown('#### Submit a class-level preference')
        _request_form(st,role,data,rows,csv_content,digest)
        st.info('Submission creates a pending request; it never modifies the timetable.')
    else:
        st.markdown('#### Review requested moves')
        _review_requests(st,data,rows,csv_content,digest,result)
        st.divider()
        with st.expander('Saved timetable versions and evaluation history',expanded=False):
            _history(st,data,digest,result)
    st.caption('GroqCloud GPT-OSS can optionally interpret unfamiliar wording in Timora. '
               'All conflict checks, room assignments and approval validation remain deterministic. '
               'Multi-class swaps and actual account authentication are future extensions.')
