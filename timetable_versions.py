"""Version selection and explicit, independently checked timetable archiving.

Generating a schedule does NOT write to the archive. The coordinator must explicitly save the selected timetable below its preview. Approved revisions are audit events and preserve
both the base and the revised CSV for traceable rollback/download.
"""
from __future__ import annotations

import json
from typing import Any

from timetable_rescheduling import schedule_hash
from timetable_records import add_version, data_digest, list_versions
from timetable_validator import validate_bytes

PASS = frozenset(('PASS', 'PASS_WITH_LIMITATIONS'))


def available_versions(result: dict) -> list[dict]:
    """Current valid distinct timetable versions available for explicit saving.

    The main (current) CSV may already be STANDARD due to fairness fallback;
    in that case do not offer a fake second 'Fairness' version.
    """
    files = result.get('files', {})
    main_valid = result.get('status_code') == 0 and result.get('independent_status') in PASS
    main = files.get('timetable_all.csv') if main_valid else None
    baseline_valid = result.get('baseline_independent_status') in PASS
    baseline = files.get('baseline_timetable_all.csv') if baseline_valid else None
    output = []
    if baseline and (not main or schedule_hash(baseline) != schedule_hash(main)):
        output.append(dict(kind='STANDARD', label='Standard CP-SAT', prefix='baseline_',
                           csv=baseline, verified_status=result.get('baseline_independent_status')))
    if main:
        kind = ('APPROVED' if result.get('rescheduled') else
                'WORKING_COPY' if result.get('loaded_saved_version') else
                'FAIRNESS' if result.get('fairness_enabled') else 'STANDARD')
        output.append(dict(kind=kind, label={
            'APPROVED': 'Updated Timetable',
            'WORKING_COPY': 'Saved working copy',
            'FAIRNESS': 'Fairness optimized',
            'STANDARD': 'Standard CP-SAT',
        }[kind], prefix='', csv=main, verified_status=result.get('independent_status')))
    return output


def active_version(result: dict, selected_label: str | None = None) -> dict:
    """Return the selected current version (not an archive item).

    A version is selected from the timetable explorer radio; default to current
    main result. Never infer a version just because a saved CSV exists.
    """
    options = available_versions(result)
    if not options:
        raise ValueError('No independently validated timetable is available')
    if selected_label == 'Standard CP-SAT':
        return next((o for o in options if o['label'] == selected_label), options[-1])
    if selected_label == 'Fairness optimized':
        return next((o for o in options if o['kind'] == 'FAIRNESS'), options[-1])
    return options[-1]


def context_result(result: dict, selected_label: str | None = None) -> dict:
    """Copy a selected version into a read-only view for workflow calculations.

    Never mutate the published result; approval applies changes to the true
    session state only after the selected baseline hash is rechecked.
    """
    chosen = active_version(result, selected_label)
    current_files = dict(result.get('files', {}))
    prefix = chosen['prefix']
    if prefix:
        current_files.update({name[len(prefix):]: contents for name, contents in current_files.items()
                              if name.startswith(prefix)})
    view = dict(result)
    view['files'] = current_files
    view['independent_status'] = chosen['verified_status']
    view['_active_source_csv_key'] = prefix + 'timetable_all.csv'
    view['_active_kind'] = chosen['kind']
    view['_active_label'] = chosen['label']
    view['fairness_enabled'] = chosen['kind'] == 'FAIRNESS'
    return view


def save_explicit_version(workbook: bytes, result: dict, kind: str) -> tuple[str, bool]:
    """Validate again, then archive exactly one explicitly selected result.

    Returns (version_id, created_new). Saving the same schedule again is idempotent.
    """
    matching = [item for item in available_versions(result) if item['kind'] == kind]
    if len(matching) != 1:
        raise ValueError(f'{kind} timetable is unavailable or not independently validated')
    version = matching[0]
    checked = validate_bytes(workbook, version['csv'],
                             result.get('include_mentor', True),
                             result.get('sim_count', 0))
    if checked.get('status') not in PASS:
        raise ValueError('The timetable failed a fresh independent validation; it was not saved')
    metrics_name = version['prefix'] + 'quality_summary.json'
    metrics_file = result.get('files', {}).get(metrics_name)
    try:
        metrics = json.loads(metrics_file.decode('utf-8')).get('metrics', {}) if metrics_file else {}
    except (ValueError, TypeError, UnicodeError):
        metrics = {}
    digest = data_digest(workbook)
    key = schedule_hash(version['csv'])
    existing = {x['schedule_hash'] for x in list_versions(digest)}
    version_id = add_version(digest, version['csv'], version['kind'], metrics)
    return version_id, key not in existing


def open_saved_as_working_copy(workbook: bytes, latest: dict, version_id: str) -> dict:
    """Rebuild an independently validated editable *copy* from an archived CSV.

    Never rewrite the original Excel workbook, archived CSV or solver output.
    Rescheduling is still a separate preview/approval workflow.
    """
    import io
    from northcap_cp_sat_solver import excel_records
    from timetable_records import get_version
    from timetable_rescheduling import csv_rows
    from timetable_workflows_ui import rebuild_verified_artifacts

    stored = get_version(data_digest(workbook), version_id)
    if not stored:
        raise ValueError('The requested saved timetable does not belong to the uploaded workbook.')
    checked = validate_bytes(workbook, stored['csv_blob'],
                             latest.get('include_mentor', True), latest.get('sim_count', 0))
    if checked.get('status') not in PASS:
        raise ValueError('The saved timetable is not valid under the currently selected Excel and generation settings.')
    data = excel_records(io.BytesIO(workbook))
    import json
    saved_metadata = json.loads(stored.get('metrics_json') or '{}')
    moved_sessions = saved_metadata.get('highlighted_session_ids', [])
    outputs, quality = rebuild_verified_artifacts(
        data, csv_rows(stored['csv_blob']),
        include_mentoring=latest.get('include_mentor', True),
        extra_labs=latest.get('sim_count', 0),
        quality_mode='saved_working_copy',
        highlighted_session_ids=moved_sessions,
        dataset_name=latest.get('input_file'), university_name=latest.get('university_name'),
        timetable_type='Saved working copy',
    )
    original_files = latest.get('files', {})
    files = {k: v for k, v in original_files.items()
             if not k.startswith('baseline_') and k not in (
                 'quality_comparison.json', 'quality_comparison.txt')}
    files.update(outputs)
    revised = dict(latest)
    revised.update({
        'files': files,
        'status_code': 0,
        'independent_status': checked['status'],
        'baseline_independent_status': 'NOT_RUN',
        'fairness_enabled': False,
        'fairness_requested': False,
        'fell_back_to_standard': False,
        'comparison_requested': False,
        'comparison': None,
        'comparison_guards': None,
        'rescheduled': False,
        'highlighted_session_ids': moved_sessions,
        'loaded_saved_version': version_id,
        'source_variant': stored['variant'],
        'logs': (latest.get('logs', '') + '\nOPENED SAVED WORKING COPY ' + version_id
                 + ': regenerated reports and independently validated CSV. '
                 'The old solver log describes the original solve, not this working copy.\n'),
    })
    return revised
