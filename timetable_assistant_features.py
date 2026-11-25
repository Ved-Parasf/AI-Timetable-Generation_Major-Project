"""Four focused, source-grounded Timetable Assistant features.

No editing occurs from model text or a room-only availability check. A class
change is published only after a complete revalidation and an explicit button
confirmation. The existing CP-SAT solver, policies and read-only chat remain.
"""
from __future__ import annotations

import csv
import io
import json

from northcap_cp_sat_solver import excel_records
from timetable_chat_moves import (
    session_label, _sessions, prepare_for_session, route_move_message, requested_destination, requested_destination_day,
    schedule_hash_from_rows,
)
from timetable_records import commit_chat_coordinator_move
from timetable_rescheduling import csv_rows, schedule_hash, public_options
from timetable_versions import context_result
from timetable_workflows_ui import rebuild_verified_artifacts


def section_for_session(data, session_row):
    """Choose a real academic parent section for the updated timetable preview.

    A mixed group can represent several source sections; the first verified
    parent is used only for navigation, never for scheduling or validation.
    """
    from timetable_report import _original_memberships
    groups = {str(r.get('group_id', '')).strip(): r
              for r in data.get('Student_Groups', [])}
    gid = str(session_row.get('group_id', '')).strip()
    if gid not in groups:
        return None
    parents = {k for k, g in groups.items()
               if str(g.get('group_kind', '')).strip() == 'PARENT'}
    if gid in parents:
        return gid
    parent = str(groups[gid].get('parent_group_id', '')).strip()
    if parent in parents:
        return parent
    possibilities = sorted(_original_memberships(groups).get(gid, ()) & parents)
    return possibilities[0] if possibilities else None


def free_room_rows(data, rows, day, period=None, room_type='All', capacity=0):
    """Every unoccupied workbook room-period pair, with no arbitrary top-N cutoff."""
    occupied = {(str(r.get('day', '')).upper(), int(float(r.get('period', 0))),
                 str(r.get('room_id', '')).strip()) for r in rows}
    results = []
    for slot in data.get('Time_Slots', []):
        d = str(slot.get('day', '')).strip().upper()
        p = int(float(slot.get('period', 0)))
        if d != day or (period is not None and p != period):
            continue
        # Room-only availability is not class-move feasibility.
        for room in data.get('Rooms', []):
            rid = str(room.get('room_id', '')).strip()
            rt = str(room.get('room_type', '')).strip()
            seats = int(float(room.get('capacity') or 0))
            if (room_type != 'All' and room_type.lower() != rt.lower()) or seats < capacity:
                continue
            if (d, p, rid) not in occupied:
                results.append({'Day': d, 'Period': p, 'Start': str(slot.get('start_time', '')),
                                'End': str(slot.get('end_time', '')), 'Room': rid,
                                'Type': rt, 'Capacity': seats})
    return sorted(results, key=lambda r: (r['Period'], r['Type'], r['Room']))


def _show_proposal(st, proposal, *, show_request_warning=True):
    if not proposal:
        return
    if proposal.get('status') != 'OPTIONS_FOUND':
        st.warning('No independently validated single-session alternative was found.')
        for reason in proposal.get('requested_reasons', []) or proposal.get('reasons', []):
            st.write('- ' + str(reason))
        for reason, count in proposal.get('other_reasons', [])[:4]:
            st.caption(f'{reason} ({count} examined slot(s))')
        return
    st.success(f"{proposal['examined_valid_options']} independently validated alternative slot(s) found.")
    st.caption(f"Current: {proposal['current_slot']} · Room {proposal['current_room']}. "
               'Every alternative keeps all other teaching sessions fixed.')
    st.dataframe([{'Slot': o['slot'], 'Room': o['room_id'],
                   'Faculty gap change': o['delta']['faculty_gaps'],
                   'Section gap change': o['delta']['section_gaps'],
                   'Soft-avoid change': o['delta']['soft_avoid_periods']}
                  for o in proposal['options']], use_container_width=True, hide_index=True,
                 height=min(410, 55 + 38 * len(proposal['options'])))
    if show_request_warning and proposal.get('requested_reasons'):
        st.warning('Requested slot: ' + '; '.join(proposal['requested_reasons'][:4]))
    st.caption('A free room or an open period alone does not establish full class-move feasibility.')


def _source_data(st, files):
    workbook = st.session_state.get('workbook_bytes')
    if not workbook:
        st.warning('Upload the original Excel workbook first.')
        return None, None
    return excel_records(io.BytesIO(workbook)), csv_rows(files['timetable_all.csv'])


def _publish(st, files, data, rows, state, choice):
    """Revalidate, save both versions in SQLite and refresh the visible report."""
    latest = st.session_state.get('solver_result')
    if not latest:
        raise ValueError('No current timetable is available')
    current = context_result(latest, st.session_state.get('timetable_variant'))
    # Read directly from the selected latest current state, not from an old
    # preview; comparison against the source hash prevents stale approvals.
    baseline = current['files']['timetable_all.csv']
    if schedule_hash(baseline) != state['base_hash'] or schedule_hash_from_rows(rows) != state['base_hash']:
        raise ValueError('The selected timetable changed. Find slots again before approving.')
    if state.get('session_id') != state['proposal'].get('session_id'):
        raise ValueError('Session selection changed; refresh the preview')
    offered = {o['slot_id']: o for o in state['proposal'].get('options', [])}
    if choice['slot_id'] not in offered or offered[choice['slot_id']] is not choice:
        raise ValueError('The selected placement was not in the verified proposal')
    moved = sorted(set(latest.get('highlighted_session_ids', [])) | {state['session_id']})
    artifacts, quality = rebuild_verified_artifacts(
        data, choice['rows'], include_mentoring=latest.get('include_mentor', True),
        extra_labs=latest.get('sim_count', 0),
        quality_mode='assistant_approved', highlighted_session_ids=moved,
        dataset_name=latest.get('input_file'), university_name=latest.get('university_name'))
    new_csv = artifacts['timetable_all.csv']
    changed_session_rows = [r for r in rows if r.get('session_id') == state['session_id']]
    if not changed_session_rows:
        raise ValueError('The selected class could not be found for the update summary')
    before_class = sorted(changed_session_rows, key=lambda r: int(float(r['period'])))
    original = before_class[0]
    section_to_show = section_for_session(data, original)
    checked = json.loads(artifacts['independent_validation.json'].decode('utf-8'))
    if checked.get('status') not in ('PASS', 'PASS_WITH_LIMITATIONS'):
        raise ValueError('The revised timetable did not pass independent validation')
    old_metrics = {}
    old_report = current['files'].get('quality_summary.json')
    if old_report:
        old_metrics = json.loads(old_report.decode('utf-8')).get('metrics', {})
    version_id = commit_chat_coordinator_move(
        st.session_state.workbook_bytes, baseline, new_csv,
        session_id=state['session_id'], destination_slot_id=choice['slot_id'],
        source_variant=current.get('_active_kind', 'STANDARD'),
        note=f"Validated assistant move to {choice['slot']}, Room {choice['room_id']}",
        metrics=dict(quality['metrics'], highlighted_session_ids=moved),
        include_mentoring=latest.get('include_mentor', True),
        extra_simulated_labs=latest.get('sim_count', 0))
    try:
        from timetable_persistence import save_workbook, read_workbook, ensure_version_files
        digest = save_workbook(st.session_state.workbook_bytes,
                               latest.get('input_file') or 'university.xlsx',
                               latest.get('university_name', ''))
        ensure_version_files(read_workbook(digest), version_id)
    except Exception as export_error:
        st.warning('Approved change saved, but PDF preparation needs retry: ' + str(export_error))
    new_files = {k: v for k, v in latest['files'].items()
                 if not (k.startswith('baseline_') or k in ('quality_comparison.json',
                                                            'quality_comparison.txt'))}
    new_files.update(artifacts)
    revised = dict(latest)
    revised.update(files=new_files, status_code=0, independent_status=checked['status'],
                   baseline_independent_status='NOT_RUN', fairness_enabled=False,
                   fairness_requested=False, comparison_requested=False,
                   comparison=None, comparison_guards=None, fell_back_to_standard=False,
                   rescheduled=True, approved_version=version_id,
                   highlighted_session_ids=moved,
                   source_variant=current.get('_active_kind', 'STANDARD'))
    revised.pop('loaded_saved_version', None)
    revised['logs'] = latest.get('logs', '') + (
        '\nCOORDINATOR APPROVED: session ' + state['session_id'] + ' moved to ' +
        choice['slot_id'] + '. The revised CSV, HTML and quality/validation reports '
        'were rebuilt and independently validated. Earlier solver logs refer '
        'to the original solve.\n')
    st.session_state.solver_result = revised
    # The previous questions described an earlier timetable version. We reset
    # their context but save a new verified action reply for this new version.
    st.session_state.assistant_history = []
    st.session_state.pop('chat_move_state', None)
    st.session_state.pop('assistant_move_reply', None)
    st.session_state.pop('assistant_move_preview', None)
    st.session_state['assistant_last_published'] = (
        f"Session {state['session_id']} moved to {choice['slot']}, Room {choice['room_id']}. "
        f"Saved revision: {version_id}.")
    original_periods = sorted({int(float(r['period'])) for r in before_class})
    previous_slot = (f"{original['day']} P{original_periods[0]}" +
                     (f"–P{original_periods[-1]}" if len(original_periods) > 1 else ''))
    confirmation = (
        f"**Updated Timetable ready.**\n\n"
        f"- **Class:** {original['course_id']} / {original['group_id']} "
        f"({original['kind']})\n"
        f"- **Previous:** {previous_slot}, Room {original['room_id']}\n"
        f"- **New placement:** {choice['slot']}, Room {choice['room_id']}\n"
        f"- **Validation:** Passed the implemented independent checks\n"
        f"- **Saved revision:** `{version_id}`\n\n"
        "The corrected timetable is now active. Open **Timetable explorer** to "
        "see the changed class highlighted in **gold** with a **MOVED** label. "
        "The previous timetable remains saved in SQLite."
    )
    st.session_state['assistant_last_published_detail'] = {
        'schedule_hash': schedule_hash(new_csv), 'version_id': version_id,
        'message': confirmation, 'course_id': original['course_id'],
        'group_id': original['group_id'], 'section': section_to_show,
    }
    st.session_state['assistant_move_notice_shown_for'] = None
    if section_to_show:
        # After closing the assistant dialog, Timetable explorer opens directly
        # on the section containing the class that was actually moved.
        st.session_state['pending_preview_section'] = section_to_show
    st.session_state['reset_timetable_variant_on_next_run'] = True
    st.session_state.coordinator_preview = None
    st.session_state.coordinator_last_diagnosis = ''
    return version_id


def _class_picker(st, rows, prefix):
    sessions = _sessions(rows)
    sid = st.selectbox('Class / teaching session', sorted(sessions),
                       format_func=lambda x: session_label(sessions[x]), key=prefix + '_session')
    return sid


def _room_panel(st, data, rows):
    st.markdown('**All available rooms**')
    days = list(dict.fromkeys(str(s.get('day', '')).upper() for s in data.get('Time_Slots', [])))
    if not days:
        st.warning('No timetable slots were found in the Excel workbook.')
        return
    col1, col2 = st.columns(2)
    day = col1.selectbox('Day', days, key='assistant_rooms_day')
    valid = [int(float(s.get('period', 0))) for s in data['Time_Slots']
             if str(s.get('day', '')).upper() == day]
    period = col2.selectbox('Period', ['All periods'] + sorted(set(valid)), key='assistant_rooms_period')
    col3, col4 = st.columns(2)
    kinds = ['All'] + sorted({str(r.get('room_type', '')).strip() for r in data.get('Rooms', [])
                              if str(r.get('room_type', '')).strip()})
    kind = col3.selectbox('Room type', kinds, key='assistant_rooms_kind')
    seats = col4.number_input('Minimum capacity', min_value=0, step=10, key='assistant_rooms_seats')
    free = free_room_rows(data, rows, day, None if period == 'All periods' else int(period),
                          kind, int(seats))
    st.write(f'**{len(free)} available room-period combinations.** No rooms are hidden.')
    if free:
        st.dataframe(free, use_container_width=True, hide_index=True, height=340)
        b = io.StringIO(newline='')
        writer = csv.DictWriter(b, fieldnames=list(free[0]))
        writer.writeheader(); writer.writerows(free)
        st.download_button('Download complete room list (CSV)', b.getvalue().encode('utf-8-sig'),
                           file_name=f'free_rooms_{day}.csv', mime='text/csv',
                           key='assistant_rooms_csv', use_container_width=True)
    st.caption('Room unallocated ≠ valid class placement. Teacher, group, duration and policies must also pass checks.')


def show_feature(st, feature, files, source):
    """Return True when an independent assistant feature was rendered."""
    if feature == 'Question Answering':
        return False
    data, rows = _source_data(st, files)
    if data is None:
        return True
    current_hash = schedule_hash_from_rows(rows)
    if feature == 'Find Free Slots':
        purpose = st.radio('Find slots for', ['A class', 'Available rooms'], horizontal=True,
                           key='assistant_slots_purpose')
        if purpose == 'Available rooms':
            _room_panel(st, data, rows)
            return True
        st.caption('Choose one exact session. Suggestions are read-only until you switch to Shift a Class and approve a move.')
        sid = _class_picker(st, rows, 'slots')
        if st.button('Find validated slots for this class', type='primary',
                     key='assistant_find_slots', use_container_width=True):
            with st.spinner('Checking all possible periods, rooms, and university rules...'):
                answer, state = prepare_for_session(
                    sid, rows, data, include_mentoring=st.session_state.solver_result.get('include_mentor', True),
                    extra_labs=st.session_state.solver_result.get('sim_count', 0), max_options=12)
            st.session_state['assistant_move_preview'] = state
        state = st.session_state.get('assistant_move_preview')
        if state and state.get('base_hash') == current_hash and state.get('session_id') == sid:
            _show_proposal(st, state['proposal'])
        return True
    if feature == 'Check Conflicts':
        st.caption('Check whether a new slot is allowed. No timetable data is changed here.')
        sid = _class_picker(st, rows, 'conflicts')
        slot_ids = [str(sl['slot_id']) for sl in data.get('Time_Slots', [])]
        if not slot_ids:
            st.warning('No timetable periods were found.')
            return True
        requested = st.selectbox('Proposed starting period', slot_ids, key='assistant_conflict_slot',
                                 format_func=lambda sid_: next((
                                     f"{sl['day']} · P{sl['period']} ({sl['start_time']}–{sl['end_time']})"
                                     for sl in data['Time_Slots'] if str(sl['slot_id']) == sid_), sid_))
        if st.button('Check conflicts', type='primary', key='assistant_conflict_check',
                     use_container_width=True):
            with st.spinner('Checking the full schedule and institutional constraints...'):
                _message, state = prepare_for_session(sid, rows, data,
                    include_mentoring=st.session_state.solver_result.get('include_mentor', True),
                    extra_labs=st.session_state.solver_result.get('sim_count', 0),
                    requested_slot=requested, max_options=12)
            st.session_state['assistant_conflict_state'] = (current_hash, sid, requested, state)
        saved = st.session_state.get('assistant_conflict_state')
        if saved and saved[:3] == (current_hash, sid, requested):
            proposal = saved[3]['proposal']
            valid = next((o for o in proposal.get('options', []) if o['slot_id'] == requested), None)
            if valid:
                st.success(f"Requested {valid['slot']} in {valid['room_id']} passed the independent checks.")
                st.caption('This is a preview; coordinator approval is still required.')
            else:
                st.warning('The requested placement is blocked. View the verified reason below.')
            # Show only ONE conflict summary here. The full explanation is
            # rendered below on demand, without another duplicate reason list.
            current_explanation = st.session_state.get('cloud_conflict_explanation')
            if not (current_explanation and current_explanation[:3] == (current_hash, sid, requested)):
                _show_proposal(st, proposal, show_request_warning=False)
            from timetable_conflict_explanations import render_checked_conflicts
            from timetable_cloud_ai import get_api_key, prioritize_conflict_reasons
            if st.button('Explain conflict and recommend a safe slot',
                         key='assistant_cloud_conflict_explanation',
                         use_container_width=True):
                from timetable_policies import load_policies
                from timetable_timora_intelligence import verified_slot_advice
                policies = load_policies(data)
                findings = {
                    'requested_slot': requested,
                    'requested_slot_allowed': bool(valid),
                    'requested_reasons': proposal.get('requested_reasons', []),
                    'validated_alternative_count': proposal.get('examined_valid_options', 0),
                }
                focus = None
                ai_on = bool(st.session_state.get('agent_use_llm', False) and get_api_key())
                if not valid and ai_on and findings['requested_reasons']:
                    with st.spinner('Examining the verified conflict reasons with Groq...'):
                        focus = prioritize_conflict_reasons(
                            findings['requested_reasons'],
                            model=st.session_state.get('agent_model', 'openai/gpt-oss-120b'))
                explanation = render_checked_conflicts(
                    findings,
                    lunch_periods=policies.lunch_periods,
                    lunch_free_count=policies.lunch_free_count,
                    focus_index=focus)
                recommendation, _, used_ai = verified_slot_advice(
                    proposal, 'Recommend the best conflict-free alternative with minimal disruption',
                    llm_enabled=ai_on,
                    model=st.session_state.get('agent_model', 'openai/gpt-oss-120b'))
                st.session_state['cloud_conflict_explanation'] = (
                    current_hash, sid, requested, explanation, recommendation,
                    (focus is not None or used_ai), used_ai)
            item = st.session_state.get('cloud_conflict_explanation')
            if item and item[:3] == (current_hash, sid, requested):
                st.markdown('**Verified conflict explanation**')
                st.markdown(item[3])
                if proposal.get('options'):
                    st.markdown('**Timora’s scheduling recommendation**')
                    st.markdown(item[4])
                st.caption(('Groq selected a verified reason/priority; Python performed all checks '
                            'and ranked only validated slots.' if item[5] else
                            'Based on Python scheduling diagnostics and verified candidate scores; '
                            'no Groq model was used for this recommendation.'))
        return True
    if feature == 'Shift a Class':
        if st.session_state.get('assistant_move_preview') and st.button(
                'Cancel class-move preview', key='assistant_shift_cancel',
                use_container_width=True):
            for key in ('assistant_move_preview','assistant_move_reply',
                        'assistant_ranked_advice','assistant_move_choice',
                        'assistant_move_approval','assistant_move_approval_for'):
                st.session_state.pop(key, None)
            st.info('Move preview cancelled. No timetable change was made.')
            st.rerun(scope='fragment')
        if source == 'active':
            notice = st.session_state.get('assistant_last_published_detail')
            if notice and notice.get('schedule_hash') == current_hash:
                st.success('Updated Timetable saved and refreshed.')
                st.markdown(notice['message'])
        if source != 'active':
            st.warning('Saved history is read-only. Open that saved timetable as a working copy in Saved runs first.')
            return True
        if st.session_state.solver_result.get('independent_status') not in ('PASS', 'PASS_WITH_LIMITATIONS'):
            st.error('Only a fully validated active timetable can be modified.')
            return True
        st.markdown('**Step 1 — Describe the class you want to move**')
        st.caption('The assistant checks actual classroom, teacher and student-group constraints. '
                   'Only the coordinator approval button can publish a change.')
        with st.form('assistant_natural_move', clear_on_submit=True):
            phrase = st.text_input('Class move request',
                placeholder='e.g. Shift CSL315 practical for BTECHCSE-V-AA to Thursday Period 1')
            send = st.form_submit_button('Find a safe move', use_container_width=True, type='primary')
        if send and phrase.strip():
            st.session_state.pop('assistant_move_choice', None)
            st.session_state.pop('assistant_move_approval', None)
            with st.spinner('Locating the class and checking the complete timetable...'):
                handled, reply, state = route_move_message(
                    phrase, rows, data, st.session_state.get('chat_move_state'),
                    include_mentoring=st.session_state.solver_result.get('include_mentor', True),
                    extra_labs=st.session_state.solver_result.get('sim_count', 0))
            st.session_state['assistant_move_reply'] = reply if handled else 'Name the class you want to shift.'
            st.session_state['chat_move_state'] = state
        if st.session_state.get('assistant_move_reply'):
            st.markdown(st.session_state.assistant_move_reply)
        state = st.session_state.get('chat_move_state')
        if not state or state.get('base_hash') != current_hash:
            return True
        if state.get('stage') == 'select_session':
            sessions = _sessions(rows)
            valid = [x for x in state['matches'] if x in sessions]
            if valid:
                sid = st.selectbox('Select the exact class to move', valid,
                                   format_func=lambda x: session_label(sessions[x]),
                                   key='assistant_select_disambiguated')
                if st.button('Check validated alternatives for this class',
                             key='assistant_select_move', use_container_width=True):
                    with st.spinner('Checking the proposed move against the full workbook...'):
                        reply, new_state = prepare_for_session(sid, rows, data,
                            include_mentoring=st.session_state.solver_result.get('include_mentor', True),
                            extra_labs=st.session_state.solver_result.get('sim_count', 0),
                            requested_slot=requested_destination(state.get('query', ''), data),
                            desired_day=requested_destination_day(state.get('query', '')), max_options=12)
                    st.session_state['chat_move_state'] = new_state
                    st.session_state.pop('assistant_move_choice', None)
                    st.session_state.pop('assistant_move_approval', None)
                    st.session_state['assistant_move_reply'] = reply
                    st.rerun(scope='fragment')
            return True
        proposal = state['proposal']
        _show_proposal(st, proposal)
        if not proposal.get('options'):
            return True
        if st.button('Ask Timora to recommend the best verified slot', key='assistant_rank_slots'):
            from timetable_timora_intelligence import verified_slot_advice
            from timetable_cloud_ai import get_api_key
            explanation, chosen, used = verified_slot_advice(
                proposal, 'Choose the best slot with minimal timetable disruption',
                llm_enabled=bool(st.session_state.get('agent_use_llm', False) and get_api_key()),
                model=st.session_state.get('agent_model', 'openai/gpt-oss-120b'))
            st.session_state['assistant_ranked_advice'] = (current_hash, state['session_id'], explanation)
            if chosen:
                state['chosen_slot'] = chosen
                st.session_state['chat_move_state'] = state
                st.session_state.pop('assistant_move_choice', None)
                st.session_state.pop('assistant_move_approval', None)
        advice = st.session_state.get('assistant_ranked_advice')
        if advice and advice[:2] == (current_hash, state['session_id']):
            st.info(advice[2])
        opts = proposal['options']
        st.markdown('**Step 2 — Select one validated alternative**')
        default_index = next((i for i, v in enumerate(opts) if v['slot_id'] == state.get('chosen_slot')),
                             proposal.get('recommended_index') or 0)
        chosen_index = st.selectbox('Validated placement to publish', range(len(opts)),
                                    index=default_index,
                                    format_func=lambda i: f"{opts[i]['slot']} · Room {opts[i]['room_id']}",
                                    key='assistant_move_choice')
        choice = opts[chosen_index]
        approval_identity = (current_hash, state['session_id'], choice['slot_id'])
        if st.session_state.get('assistant_move_approval_for') != approval_identity:
            st.session_state['assistant_move_approval_for'] = approval_identity
            st.session_state.pop('assistant_move_approval', None)
        st.info('The selected class will move. Other classes remain unchanged. '
                'The original timetable will be preserved in SQLite history, and the moved class '
                'will appear highlighted in the refreshed timetable report.')
        st.markdown('**Step 3 — Approve and publish the corrected timetable**')
        approval = st.checkbox('I am the demo timetable coordinator and I approve this validated class change',
                               key='assistant_move_approval')
        if st.button('Confirm and update timetable', type='primary',
                     disabled=not approval, key='assistant_publish_move', use_container_width=True):
            try:
                with st.spinner('Validating the complete revised timetable and recording the version...'):
                    _publish(st, files, data, rows, state, choice)
                st.rerun()
            except Exception as exc:
                st.error('Timetable was not updated: ' + str(exc))
        return True
    return False


def show_chat_move_controls(st, files, source):
    """Expose existing validator/approval workflow directly in Timora chat.

    No LLM text or typed 'approve' can commit a timetable change. Choosing
    one of the independently verified options and clicking the explicitly
    authorized publish button is required, and _publish revalidates.
    """
    if source != 'active':
        st.warning('Historical timetables are read-only. Open a saved run as a working copy first.')
        return
    latest = st.session_state.get('solver_result') or {}
    if latest.get('independent_status') not in ('PASS', 'PASS_WITH_LIMITATIONS'):
        st.error('Only an independently validated working timetable can be changed.')
        return
    state = st.session_state.get('chat_move_state')
    if not state:
        return
    if st.button('Cancel class move — keep timetable unchanged',
                 key='timora_chat_cancel_move', use_container_width=True):
        for key in ('chat_move_state','assistant_move_reply','assistant_move_preview',
                    'assistant_ranked_advice','assistant_move_choice','assistant_move_approval',
                    'timora_chat_slot','timora_chat_approval','timora_chat_choice_identity',
                    'timora_chat_approval_identity'):
            st.session_state.pop(key, None)
        cancel_msg = 'Class-move preview cancelled. No timetable change was made.'
        st.session_state.assistant_history.append({'role':'assistant','content':cancel_msg})
        if st.session_state.get('assistant_chat_thread_id'):
            from timetable_chat_history import append_messages
            try:
                append_messages(st.session_state['assistant_chat_workbook_digest'],
                                st.session_state['assistant_chat_thread_id'],
                                st.session_state['assistant_chat_source_key'],
                                [{'role':'assistant','content':cancel_msg}])
            except (OSError,ValueError):
                pass  # The current in-memory confirmation remains visible.
        st.rerun(scope='fragment')
    data, rows = _source_data(st, files)
    if data is None:
        return
    current_hash = schedule_hash(files['timetable_all.csv'])
    if state.get('base_hash') != current_hash:
        st.warning('Your timetable has changed. Ask Timora to find fresh alternatives.')
        return
    if state.get('stage') == 'need_class':
        st.caption('Tell Timora the course and section in the chat box to select an existing class.')
        return
    if state.get('stage') == 'select_session':
        sessions = _sessions(rows)
        matches = [sid for sid in state.get('matches', ()) if sid in sessions]
        if not matches:
            st.warning('That class is no longer present. Start a fresh request.')
            return
        sid = st.selectbox('Choose the existing teaching session', matches,
                           format_func=lambda x: session_label(sessions[x]),
                           key='timora_chat_session')
        if st.button('Find verified placements', key='timora_chat_check', use_container_width=True):
            with st.spinner('Checking teacher, student-group, room and workbook policy rules...'):
                reply, revised = prepare_for_session(
                    sid, rows, data, include_mentoring=latest.get('include_mentor', True),
                    extra_labs=latest.get('sim_count', 0),
                    requested_slot=requested_destination(state.get('query', ''), data),
                    desired_day=requested_destination_day(state.get('query', '')),
                    max_options=12)
            st.session_state['chat_move_state'] = revised
            st.session_state['assistant_move_reply'] = reply
            st.session_state.assistant_history.append({'role':'assistant', 'content': reply})
            st.session_state.pop('timora_chat_slot', None)
            st.session_state.pop('timora_chat_approval', None)
            st.rerun(scope='fragment')
        return
    if state.get('stage') != 'options':
        return
    proposal = state.get('proposal', {})
    if not proposal.get('options'):
        _show_proposal(st, proposal)
        return
    options = proposal['options']
    st.caption(f"{proposal['examined_valid_options']} independently validated alternatives. "
               "All other teaching sessions remain unchanged.")
    if st.button('Timora: recommend a validated placement', key='timora_chat_recommend',
                 use_container_width=True):
        from timetable_timora_intelligence import verified_slot_advice
        from timetable_cloud_ai import get_api_key
        with st.spinner('Comparing only independently validated alternatives...'):
            advice, chosen, _ = verified_slot_advice(
                proposal, 'Choose the best verified slot with minimal disruption',
                llm_enabled=bool(st.session_state.get('agent_use_llm', False) and get_api_key()),
                model=st.session_state.get('agent_model', 'openai/gpt-oss-120b'))
        previous = st.session_state.assistant_history[-1] if st.session_state.assistant_history else None
        if not previous or previous.get('role') != 'assistant' or previous.get('content') != advice:
            st.session_state.assistant_history.append({'role':'assistant', 'content': advice})
        if chosen:
            state['chosen_slot'] = chosen
            st.session_state['chat_move_state'] = state
            st.session_state.pop('timora_chat_slot', None)
            st.session_state.pop('timora_chat_approval', None)
        st.rerun(scope='fragment')
    # Clear a stale dropdown when a new session or preferred slot is selected.
    identity = (current_hash, state['session_id'], state.get('chosen_slot'))
    if st.session_state.get('timora_chat_choice_identity') != identity:
        st.session_state['timora_chat_choice_identity'] = identity
        st.session_state.pop('timora_chat_slot', None)
        st.session_state.pop('timora_chat_approval', None)
    default = next((i for i, opt in enumerate(options)
                    if opt['slot_id'] == state.get('chosen_slot')),
                   proposal.get('recommended_index') or 0)
    selected = st.selectbox('Choose a verified new placement', range(len(options)),
                            index=default, key='timora_chat_slot',
                            format_func=lambda i: f"{options[i]['slot']} · Room {options[i]['room_id']}")
    choice = options[selected]
    approval_id = (current_hash, state['session_id'], choice['slot_id'])
    if st.session_state.get('timora_chat_approval_identity') != approval_id:
        st.session_state['timora_chat_approval_identity'] = approval_id
        st.session_state.pop('timora_chat_approval', None)
    st.caption('The selected class will move as one session, including both practical periods if applicable. '
               'The original version stays in local history. No other class is moved.')
    approved = st.checkbox('I approve this verified timetable change as coordinator',
                           key='timora_chat_approval')
    if st.button('Apply change and open Updated Timetable',
                 disabled=not approved, type='primary', key='timora_chat_publish',
                 use_container_width=True):
        try:
            with st.spinner('Revalidating the full timetable and saving a new version...'):
                _publish(st, files, data, rows, state, choice)
            st.session_state['studio_page'] = 'results'
            st.rerun()
        except Exception as exc:
            st.error('The timetable was not changed: ' + str(exc))
