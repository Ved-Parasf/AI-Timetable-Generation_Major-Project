"""Read-only analytics dashboard for independently validated Timora timetables.

All figures originate in existing quality_summary.json files written by
`timetable_fairness.quality_report`.  This module does not create schedules,
change weights, call an LLM or modify saved versions.
"""
from __future__ import annotations

import csv
import io
import json

PASS = frozenset(('PASS', 'PASS_WITH_LIMITATIONS'))
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri')
# One shared, explicit palette across every comparison and single-version chart.
STANDARD_BLUE = '#2563EB'
FAIRNESS_GREEN = '#16A34A'
VERSION_COLORS = {'Standard': STANDARD_BLUE, 'Fairness': FAIRNESS_GREEN}


def version_color(is_fairness: bool) -> str:
    """Use the same version color for every chart, regardless of metric type."""
    return FAIRNESS_GREEN if is_fairness else STANDARD_BLUE

# Metrics are periods/proxies, not student-level attendance measurements.
METRICS = (
    ('teacher_gap_periods', 'Faculty gaps'),
    ('section_avoidable_gap_proxy', 'Student-section gaps (proxy)'),
    ('teacher_daily_overload_periods', 'Faculty daily overload'),
    ('late_session_periods', 'Late teaching periods'),
    ('preferred_free_slot_periods', 'Policy soft-avoid periods'),
    ('total_teacher_weekday_load_spread', 'Faculty weekday load spread'),
)


def verified_quality(result: dict | None, prefix: str = '') -> dict | None:
    """Never chart an unvalidated solver output as verified data."""
    if not isinstance(result, dict):
        return None
    status = (result.get('baseline_independent_status') if prefix
              else result.get('independent_status'))
    if status not in PASS:
        return None
    if not prefix and result.get('status_code') != 0:
        return None
    raw = result.get('files', {}).get(prefix + 'quality_summary.json')
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get('metrics'), dict):
        return None
    return payload


def resolve_live_reports(result: dict, standard_result: dict | None = None,
                         *, same_workbook: bool = False) -> tuple[dict | None, dict | None, str]:
    """Get Standard/Fairness sources without mixing incompatible datasets.

    A fairness run is paired with its embedded validated baseline when present,
    otherwise with the separately retained validated Standard (same workbook
    and mentoring/lab settings). A Standard-only run stays Standard-only.
    """
    standard = verified_quality(result, 'baseline_')
    fairness = (verified_quality(result) if result.get('fairness_enabled')
                and not result.get('fell_back_to_standard') else None)
    if standard is not None:
        label = 'Standard and Fairness from the same generation'
    else:
        label = 'Current independently validated timetable'
    if standard is None and same_workbook and standard_result:
        if (result.get('include_mentor') == standard_result.get('include_mentor')
                and result.get('sim_count') == standard_result.get('sim_count')):
            standard = verified_quality(standard_result)
            if standard is not None and fairness is not None:
                label = 'Same-workbook Standard baseline and Fairness run'
    if standard is None and fairness is None:
        standard = verified_quality(result)
    return standard, fairness, label


def comparison_rows(standard: dict | None, fairness: dict | None) -> list[dict]:
    """Only compare metrics that were actually measured in *both* reports."""
    if not standard or not fairness:
        return []
    before = standard.get('metrics', {})
    after = fairness.get('metrics', {})
    output = []
    for key, label in METRICS:
        a, b = before.get(key), after.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            output.append({'metric': label, 'standard': a, 'fairness': b,
                           'change': b - a})
    return output


def faculty_names(csv_blob: bytes | str | None) -> dict[str, str]:
    """Use names only from the displayed validated timetable CSV."""
    if not csv_blob:
        return {}
    if isinstance(csv_blob, bytes):
        csv_blob = csv_blob.decode('utf-8-sig', errors='replace')
    names = {}
    for row in csv.DictReader(io.StringIO(csv_blob)):
        fid = (row.get('faculty_id') or '').strip()
        name = (row.get('faculty_name') or '').strip()
        if fid and name:
            names.setdefault(fid, name)
    return names


def daily_totals(report: dict, entity: str = 'faculty') -> dict[str, int]:
    rows = report.get('faculty' if entity == 'faculty' else 'sections', [])
    return {day.upper(): sum(int(r.get(day) or 0) for r in rows) for day in DAYS}


def archive_pairs(versions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Saved versions are scoped to the uploaded workbook digest by caller.

    Existing automatic saves contain settings/metadata but not measured metrics;
    reconstruct quality reports from CSV only after explicit user request.
    """
    return ([v for v in versions if v.get('variant') == 'STANDARD'],
            [v for v in versions if v.get('variant') == 'FAIRNESS'])


def _bar(st, data, *, height=290, colors=None, horizontal=False):
    kwargs = {'use_container_width': True, 'height': height, 'horizontal': horizontal}
    if colors:
        kwargs['color'] = colors
    st.bar_chart(data, **kwargs)


def _metric_short_label(label: str) -> str:
    mapping = {
        'Faculty gaps': 'Faculty gaps',
        'Student-section gaps (proxy)': 'Student gaps',
        'Faculty daily overload': 'Faculty overload',
        'Late teaching periods': 'Late periods',
        'Policy soft-avoid periods': 'Soft-avoid periods',
        'Faculty weekday load spread': 'Weekday load spread',
    }
    return mapping.get(label, label)


def _line_comparison(st, pair, *, height=380):
    import pandas as pd
    import altair as alt
    if not pair:
        return
    rows = []
    metric_order = []
    for p in pair:
        metric = _metric_short_label(p['metric'])
        metric_order.append(metric)
        rows.append({'Metric': metric, 'Type': 'Standard', 'Value': p['standard']})
        rows.append({'Metric': metric, 'Type': 'Fairness', 'Value': p['fairness']})
    df = pd.DataFrame(rows)
    chart = alt.Chart(df).mark_line(point=True, strokeWidth=3).encode(
        x=alt.X('Metric:N', sort=metric_order,
                axis=alt.Axis(title='', labelAngle=-30, labelLimit=175, labelPadding=12, labelFontSize=11)),
        y=alt.Y('Value:Q', title='Measured periods / proxy count'),
        color=alt.Color('Type:N', scale=alt.Scale(domain=['Standard', 'Fairness'],
                                                 range=[STANDARD_BLUE, FAIRNESS_GREEN]),
                        legend=alt.Legend(title='Timetable')),
        tooltip=['Metric:N', 'Type:N', 'Value:Q']
    ).properties(height=height)
    st.altair_chart(chart, use_container_width=True)


def _summary_insights(pair: list[dict]) -> list[str]:
    if not pair:
        return []
    insights = []
    # Lower is better for all metrics currently charted.
    improved = [p for p in pair if p['fairness'] < p['standard']]
    worsened = [p for p in pair if p['fairness'] > p['standard']]
    unchanged = [p for p in pair if p['fairness'] == p['standard']]
    if improved:
        best = max(improved, key=lambda p: p['standard'] - p['fairness'])
        insights.append(
            f"Fairness is better on {len(improved)} of {len(pair)} comparison metrics. "
            f"Largest improvement: {_metric_short_label(best['metric'])} improved by {best['standard'] - best['fairness']} periods."
        )
    if worsened:
        worst = max(worsened, key=lambda p: p['fairness'] - p['standard'])
        insights.append(
            f"Watch-out metric: {_metric_short_label(worst['metric'])} is worse by {worst['fairness'] - worst['standard']} periods in the Fairness timetable."
        )
    if unchanged:
        insights.append(f'{len(unchanged)} metric(s) were unchanged between the timetables.')
    return insights


def render_analytics_dashboard(st, result: dict, standard_result: dict | None,
                     *, same_workbook: bool = False,
                     archive_versions: list[dict] | None = None,
                     workbook_blob: bytes | None = None,
                     workbook_name: str = '', university_name: str = '') -> None:
    """Render responsive Streamlit charts without affecting solver state."""
    import pandas as pd

    standard, fairness, provenance = resolve_live_reports(
        result, standard_result, same_workbook=same_workbook)
    current = fairness or standard
    if current is None:
        st.info('Analytics require an independently validated timetable and quality summary.')
        return
    st.markdown('''<div class="analytics-hero">
      <span class="analytics-eyebrow">TIMORA / ANALYTICS STUDIO</span>
      <h2>Analytics Dashboard</h2>
      <p>Explore faculty workload, timetable gaps, and daily distribution for the
      selected independently validated timetable. This view does not compare versions.</p>
    </div>''', unsafe_allow_html=True)
    st.caption('Showing: ' + ('🟢 Fairness timetable' if fairness else '🔵 Standard timetable') + '. This view describes one timetable only. '
               'Open Standard vs Fairness for before-and-after graphs. Data are measured, not AI predictions.')

    active_color = version_color(fairness is not None)
    metrics = current.get('metrics', {})
    def metric_value(key):
        value = metrics.get(key)
        return int(value) if isinstance(value, (int, float)) else None

    kpis = st.columns(4)
    for col, key, title in zip(
        kpis,
        ('teacher_gap_periods', 'section_avoidable_gap_proxy',
         'teacher_daily_overload_periods', 'late_session_periods'),
        ('Faculty gap periods', 'Student-section gap proxy',
         'Faculty overload periods', 'Late teaching periods')):
        cur = metric_value(key)
        col.metric(title, cur if cur is not None else '—',
                   help='Periods computed from the currently displayed validated timetable.')

    st.markdown('#### Faculty & student-section gap analysis')
    gap1, gap2 = st.columns(2)
    name_source = (result.get('files', {}).get('timetable_all.csv')
                   if fairness else (standard_result or result).get('files', {}).get('timetable_all.csv'))
    names = faculty_names(name_source)
    faculty = current.get('faculty', [])
    sections = current.get('sections', [])
    with gap1:
        st.markdown('**Faculty with the most timetable gaps**')
        top = sorted(faculty, key=lambda r: int(r.get('gap_periods') or 0), reverse=True)[:12]
        if top:
            labels = [names.get(r.get('faculty_id'), r.get('faculty_id', 'Unknown')) for r in top]
            # Disambiguate duplicate faculty names when present.
            labels = [f'{n} ({r.get("faculty_id", "")})' for n, r in zip(labels, top)]
            _bar(st, pd.DataFrame({'Faculty': labels,
                                   'Gap periods': [int(r.get('gap_periods') or 0) for r in top]}).set_index('Faculty'),
                 height=385, colors=[active_color], horizontal=True)
        else:
            st.caption('No faculty-level gap details are available.')
    with gap2:
        st.markdown('**Student sections with the most avoidable gaps**')
        top = sorted(sections, key=lambda r: int(r.get('avoidable_gap_proxy') or 0), reverse=True)[:12]
        if top:
            _bar(st, pd.DataFrame({'Section': [r.get('section_id', 'Unknown') for r in top],
                    'Avoidable gaps': [int(r.get('avoidable_gap_proxy') or 0) for r in top]}).set_index('Section'),
                 height=385, colors=[active_color], horizontal=True)
        else:
            st.caption('No section-gap details are available.')

    st.markdown('#### Teacher workload & weekly distribution')
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown('**Highest weekly teaching loads**')
        top = sorted(faculty, key=lambda r: int(r.get('weekly_periods') or 0), reverse=True)[:12]
        if top:
            labels = [f'{names.get(r.get("faculty_id"), r.get("faculty_id", "Unknown"))} ({r.get("faculty_id", "")})'
                      for r in top]
            _bar(st, pd.DataFrame({'Faculty': labels,
                  'Teaching periods': [int(r.get('weekly_periods') or 0) for r in top]}).set_index('Faculty'),
                 height=385, colors=[active_color], horizontal=True)
    with col_b:
        st.markdown('**Faculty teaching periods by weekday**')
        by_day = daily_totals(current)
        _bar(st, pd.DataFrame({'Day': list(by_day), 'Occupied teaching periods': list(by_day.values())})
             .set_index('Day'), height=305, colors=[active_color])
    st.caption('Workload counts scheduled **periods**, not independent teaching sessions. '
               'Fixed weekly faculty assignments cannot be redistributed between faculty by this solver.')

    with st.expander('Detailed faculty daily load and section gaps', expanded=False):
        if faculty:
            daily = pd.DataFrame([{
                'Faculty': names.get(row.get('faculty_id'), row.get('faculty_id', 'Unknown')),
                'Weekly periods': int(row.get('weekly_periods') or 0),
                **{day.upper(): int(row.get(day) or 0) for day in DAYS},
                'Gap periods': int(row.get('gap_periods') or 0),
                'Overload periods': int(row.get('overload_above_daily_target') or 0),
            } for row in faculty])
            st.dataframe(daily.sort_values(['Weekly periods', 'Gap periods'], ascending=False),
                         use_container_width=True, hide_index=True)
        if sections:
            st.dataframe(pd.DataFrame(sections).rename(columns={
                'section_id': 'Section', 'avoidable_gap_proxy': 'Avoidable gap proxy'}),
                use_container_width=True, hide_index=True)

    export = io.StringIO()
    writer = csv.writer(export)
    writer.writerow(['Metric', 'Current timetable'])
    for key, label in METRICS:
        if key in metrics:
            writer.writerow([label, metrics[key]])
    st.download_button('Download analytics metrics (CSV)', export.getvalue().encode('utf-8-sig'),
                       file_name='timora_analytics_current.csv', mime='text/csv',
                       key='download_analytics_csv')
    st.caption('Interpretation limits: section gaps are a proxy based on modeled parent sections '
               'and lab batches, not student-ID-level gaps. Missing individual elective memberships, '
               'provisional allocations and any validation limitations remain in force. '
               'FEASIBLE does not mean optimal.')


# Keep the former public entry point for callers outside this patch.
def render_dashboard(st, result: dict, standard_result: dict | None, **kwargs) -> None:
    return render_analytics_dashboard(st, result, standard_result, **kwargs)


def entity_comparison(standard: dict, fairness: dict, entity: str) -> list[dict]:
    """Compare only identities that exist in BOTH verified schedules.

    Each row is an identified faculty or section, never a positional join.
    Missing entities are intentionally not treated as zero-load staff/sections.
    """
    if entity not in ('faculty', 'sections'):
        raise ValueError('entity must be faculty or sections')
    identifier = 'faculty_id' if entity == 'faculty' else 'section_id'
    metric_key = 'gap_periods' if entity == 'faculty' else 'avoidable_gap_proxy'
    before = {str(r[identifier]): r for r in standard.get(entity, []) if r.get(identifier)}
    after = {str(r[identifier]): r for r in fairness.get(entity, []) if r.get(identifier)}
    results = []
    for item_id in sorted(before.keys() & after.keys()):
        a, b = before[item_id], after[item_id]
        if not all(isinstance(r.get(metric_key), (int, float)) for r in (a,b)):
            continue
        results.append({
            'id': item_id,
            'standard_gaps': a[metric_key],
            'fairness_gaps': b[metric_key],
            'gap_change': b[metric_key] - a[metric_key],
            'standard_weekly': a.get('weekly_periods' if entity == 'faculty' else 'weekly_occupied_periods'),
            'fairness_weekly': b.get('weekly_periods' if entity == 'faculty' else 'weekly_occupied_periods'),
            'standard_overload': a.get('overload_above_daily_target') if entity == 'faculty' else None,
            'fairness_overload': b.get('overload_above_daily_target') if entity == 'faculty' else None,
            'standard_spread': a.get('weekday_load_spread') if entity == 'faculty' else None,
            'fairness_spread': b.get('weekday_load_spread') if entity == 'faculty' else None,
            'standard_daily': {d.upper(): int(a.get(d) or 0) for d in DAYS},
            'fairness_daily': {d.upper(): int(b.get(d) or 0) for d in DAYS},
        })
    return results


def _render_entity_comparison(st, standard, fairness, *, entity: str, names: dict[str,str], suffix: str = '') -> None:
    import pandas as pd
    import altair as alt

    rows = entity_comparison(standard, fairness, entity)
    is_faculty = entity == 'faculty'
    entity_title = 'Faculty' if is_faculty else 'Student section'
    if not rows:
        st.info(f'No matched {entity_title.lower()}-level records are available for before/after analysis.')
        return
    unmatched_standard = len(standard.get(entity, [])) - len(rows)
    unmatched_fairness = len(fairness.get(entity, [])) - len(rows)
    if unmatched_standard or unmatched_fairness:
        st.warning(f'{unmatched_standard} Standard and {unmatched_fairness} Fairness '
                   f'{entity_title.lower()} row(s) could not be matched. Only matching IDs are compared.')

    for r in rows:
        name = names.get(r['id'], r['id']) if is_faculty else r['id']
        r['display'] = f'{name} ({r["id"]})' if name != r['id'] else r['id']

    title = 'Faculty gap periods — before vs after' if is_faculty else 'Section avoidable gap proxy — before vs after'
    st.markdown(f'**{title}**')
    # Rank by magnitude of change, then by largest existing gap. Show full data in sortable tables below.
    ranked = sorted(rows, key=lambda r: (abs(r['gap_change']), max(r['standard_gaps'],r['fairness_gaps'])), reverse=True)
    plot_rows = ranked[:15]
    data = pd.DataFrame([{'Name': r['display'], 'Version': label, 'Gaps': r[key]}
                         for r in plot_rows
                         for label,key in (('Standard','standard_gaps'), ('Fairness','fairness_gaps'))])
    order = [r['display'] for r in reversed(plot_rows)]
    chart = alt.Chart(data).mark_bar().encode(
        y=alt.Y('Name:N', sort=order, title='', axis=alt.Axis(labelLimit=260)),
        x=alt.X('Gaps:Q', title='Gap periods (lower is better)'),
        yOffset=alt.YOffset('Version:N'),
        color=alt.Color('Version:N', scale=alt.Scale(domain=['Standard','Fairness'],
                                                   range=[STANDARD_BLUE, FAIRNESS_GREEN])),
        tooltip=['Name:N','Version:N','Gaps:Q'],
    ).properties(height=max(300, len(plot_rows)*28))
    st.altair_chart(chart, use_container_width=True)
    st.caption('Shows up to 15 entries with the largest before/after differences. All matched entries are in the table below.')

    st.markdown(f'**Inspect one {entity_title.lower()} — daily before and after**')
    selection = st.selectbox(f'Select {entity_title.lower()}',
                             [r['id'] for r in rows],
                             format_func=lambda key: next(x['display'] for x in rows if x['id']==key),
                             key='fairness_compare_select_'+entity+suffix)
    chosen = next(r for r in rows if r['id']==selection)
    day_rows = [{'Day': day, 'Version': version, 'Teaching periods': daily[day]}
                for day in (d.upper() for d in DAYS)
                for version,daily in (('Standard',chosen['standard_daily']),
                                      ('Fairness',chosen['fairness_daily']))]
    daily_chart = alt.Chart(pd.DataFrame(day_rows)).mark_line(point=True, strokeWidth=3).encode(
        x=alt.X('Day:N', sort=[d.upper() for d in DAYS], axis=alt.Axis(labelAngle=0), title='Weekday'),
        y=alt.Y('Teaching periods:Q', title='Scheduled periods', scale=alt.Scale(zero=True)),
        color=alt.Color('Version:N', scale=alt.Scale(domain=['Standard','Fairness'],
                                                   range=[STANDARD_BLUE, FAIRNESS_GREEN])),
        tooltip=['Day:N','Version:N','Teaching periods:Q']
    ).properties(height=240)
    st.altair_chart(daily_chart, use_container_width=True)
    a,b = chosen['standard_gaps'], chosen['fairness_gaps']
    st.caption(f'For {chosen["display"]}: gaps {a} → {b} (change {b-a:+}). '
               'The daily line chart shows number of scheduled periods, not the timing of individual gaps.')

    table = []
    for r in sorted(rows, key=lambda x: (-abs(x['gap_change']), x['display'])):
        row={'Faculty' if is_faculty else 'Section': r['display'],
             'Standard gaps': r['standard_gaps'],
             'Fairness gaps': r['fairness_gaps'],
             'Gap change': r['gap_change'],
             'Standard weekly periods': r['standard_weekly'],
             'Fairness weekly periods': r['fairness_weekly']}
        if is_faculty:
            row.update({'Standard overload': r['standard_overload'],
                        'Fairness overload': r['fairness_overload'],
                        'Standard weekday spread': r['standard_spread'],
                        'Fairness weekday spread': r['fairness_spread']})
        table.append(row)
    with st.expander(f'All {len(rows)} matched {entity_title.lower()} before/after values', expanded=False):
        df = pd.DataFrame(table)
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.download_button(f'Download {entity_title.lower()} comparison (CSV)',
                           df.to_csv(index=False).encode('utf-8-sig'),
                           file_name=f'timora_fairness_{entity}_comparison.csv',
                           mime='text/csv', key='fairness_download_'+entity+suffix)


def _render_pair(st, standard: dict, fairness: dict, *, names: dict[str,str], suffix: str = '') -> None:
    import pandas as pd
    pair = comparison_rows(standard, fairness)
    if not pair:
        st.warning('No matching measured Standard/Fairness metrics are available.')
        return
    st.markdown('#### Overall Standard vs Fairness')
    st.caption('Lower is better for these gap, overload and policy-penalty counts. '
               'The line chart connects different metrics only for visual comparison; '
               'the metrics have different scales and must be interpreted individually.')
    _line_comparison(st, pair, height=340)
    st.markdown('**What changed?**')
    for insight in _summary_insights(pair):
        st.write('• '+insight)
    st.dataframe(pd.DataFrame([{
        'Metric':_metric_short_label(r['metric']), 'Standard':r['standard'],
        'Fairness':r['fairness'], 'Fairness minus Standard':r['change']
    } for r in pair]), hide_index=True, use_container_width=True)
    st.caption('Negative differences indicate fewer counted gaps/penalties; a single metric does not establish overall optimization superiority.')
    st.markdown('#### Faculty — before and after')
    _render_entity_comparison(st, standard, fairness, entity='faculty', names=names, suffix=suffix)
    st.markdown('#### Student sections — before and after')
    _render_entity_comparison(st, standard, fairness, entity='sections', names={}, suffix=suffix)
    export = io.StringIO()
    writer = csv.writer(export)
    writer.writerow(['Metric', 'Standard', 'Fairness', 'Fairness minus Standard'])
    for r in pair:
        writer.writerow([r['metric'], r['standard'], r['fairness'], r['change']])
    st.download_button('Download Standard vs Fairness summary (CSV)',
                       export.getvalue().encode('utf-8-sig'),
                       file_name='timora_fairness_comparison.csv', mime='text/csv',
                       key='fairness_summary_download'+suffix)


def render_fairness_dashboard(st, result: dict, standard_result: dict | None,
                              *, same_workbook: bool = False,
                              archive_versions: list[dict] | None = None,
                              workbook_blob: bytes | None = None,
                              workbook_name: str = '', university_name: str = '') -> None:
    """Render only before/after Fairness charts, never current-only Analytics graphs."""
    import hashlib
    standard, fairness, provenance = resolve_live_reports(
        result, standard_result, same_workbook=same_workbook)
    st.markdown('### Standard vs Fairness dashboard')
    st.caption('🔵 Standard = blue · 🟢 Fairness = green — colors stay the same in every comparison chart.')
    st.caption('Before-and-after comparisons use independently validated quality reports. '
               'A Student-section gap is a modeled proxy; no individual student attendance is inferred.')
    if standard and fairness:
        st.caption('Comparison source: '+provenance)
        name_blob = (result.get('files',{}).get('baseline_timetable_all.csv')
                     or (standard_result or {}).get('files',{}).get('timetable_all.csv')
                     or result.get('files',{}).get('timetable_all.csv'))
        _render_pair(st, standard, fairness, names=faculty_names(name_blob))
    else:
        st.info('A validated Standard and Fairness pair is not available in this run. '
                'Generate Fairness from a validated Standard timetable to see the before/after charts. '
                'You can also compare matching archived versions below.')
    previous_std, previous_fair = archive_pairs(archive_versions or [])
    if previous_std and previous_fair and workbook_blob:
        with st.expander('Compare saved Standard and Fairness versions', expanded=False):
            st.caption('Saved runs may use different solver time budgets and search settings. '
                       'Differences alone do not prove causality.')
            left,right = st.columns(2)
            version_label=lambda v: f"{v.get('created_at','')} · {v.get('version_id','')}"
            with left:
                archived_std=st.selectbox('Saved Standard', previous_std,
                    format_func=version_label, key='fairness_saved_standard')
            with right:
                archived_fair=st.selectbox('Saved Fairness', previous_fair,
                    format_func=version_label, key='fairness_saved_fairness')
            if st.button('Load and validate saved comparison', key='fairness_load_saved'):
                try:
                    from timetable_persistence import restore_version
                    workbook_entry={'workbook_blob':workbook_blob, 'filename':workbook_name,
                                    'university_name':university_name}
                    s_result=restore_version(workbook_entry, archived_std['version_id'])
                    f_result=restore_version(workbook_entry, archived_fair['version_id'])
                    s_quality=verified_quality(s_result)
                    f_quality=verified_quality(f_result)
                    if not s_quality or not f_quality:
                        st.error('Both saved versions must pass independent validation.')
                    else:
                        st.session_state['fairness_historical_pair'] = {
                            'workbook_hash': hashlib.sha256(workbook_blob).hexdigest(),
                            'standard_id': archived_std['version_id'],
                            'fairness_id': archived_fair['version_id'],
                            'standard': s_quality,'fairness': f_quality,
                            'standard_csv': s_result.get('files',{}).get('timetable_all.csv'),
                            'standard_settings': (s_result.get('include_mentor'),s_result.get('sim_count')),
                            'fairness_settings': (f_result.get('include_mentor'),f_result.get('sim_count')),
                        }
                except (ImportError, ValueError, KeyError, TypeError, OSError) as exc:
                    st.error('Saved comparison could not be loaded: '+str(exc))
        historical=st.session_state.get('fairness_historical_pair')
        if historical:
            matches=(historical.get('workbook_hash')==hashlib.sha256(workbook_blob).hexdigest()
                     and historical.get('standard_id')==archived_std['version_id']
                     and historical.get('fairness_id')==archived_fair['version_id'])
            if matches:
                if historical.get('standard_settings')!=historical.get('fairness_settings'):
                    st.warning('The selected versions used different mentoring/lab settings; '
                               'a like-for-like comparison cannot be shown.')
                else:
                    st.markdown('### Archived Standard vs Fairness comparison')
                    _render_pair(st, historical['standard'],historical['fairness'],
                                 names=faculty_names(historical.get('standard_csv')),suffix='_historical')
    st.caption('Validation limitations: provisional assignments and missing individual elective '
               'enrolments may prevent verification of real-world student clashes. '
               'CP-SAT FEASIBLE does not mean mathematically optimal.')
