"""UCB1-guided adaptive neighborhood Fairness search. No Groq and no hard-rule edits.

Each arm releases actual timetable sessions selected by timetable_seed. Every
attempt calls the existing CP-SAT solver and independent validator. Only a
strictly better, independently validated solution becomes the next incumbent.
Both successes and failures are retained in SQLite for verifiable learning.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from timetable_fairness import fairness_score, weights_for_profile
from timetable_records import db_path

OPERATORS = {
    'faculty_gap_repair': 'Repair faculty timetable gaps',
    'section_gap_repair': 'Repair student-section timetable gaps',
    'workload_balance_repair': 'Balance faculty teaching days',
    'late_period_repair': 'Reconsider late-period classes',
}
VALID = ('PASS', 'PASS_WITH_LIMITATIONS')
# Coordinator-adjustable prototype preferences; never overrides feasibility.
DEFAULT_CHANGE_COSTS = {'room': 1, 'theory': 3, 'practical': 5}


def validate_change_costs(costs=None):
    weights = dict(DEFAULT_CHANGE_COSTS if costs is None else costs)
    if set(weights) != set(DEFAULT_CHANGE_COSTS) or any(
            type(value) is not int or value < 1 or value > 30 for value in weights.values()):
        raise ValueError('Change costs must be positive bounded integers for room, theory and practical')
    return weights


def _change_cost(change, costs):
    time_moved = change['change_type'] != 'room_only'
    room_moved = change['change_type'] in ('time_and_room', 'room_only')
    kind = str(change.get('session_kind') or 'THEORY').upper()
    return ((costs['practical'] if kind == 'PRACTICAL' else costs['theory']) if time_moved else 0) + (costs['room'] if room_moved else 0)



def learning_context(*, seconds, fraction, profile, priority, include_mentor, extra_labs,
                     max_change_fraction=None, target_improvement_percent=None, change_costs=None):
    weights_for_profile(profile)
    costs = validate_change_costs(change_costs)
    if target_improvement_percent is not None and not 0 < float(target_improvement_percent) <= 100:
        raise ValueError('Invalid fairness-improvement target')
    values = (int(seconds), round(float(fraction), 5), profile, priority,
              bool(include_mentor), int(extra_labs),
              (round(float(max_change_fraction), 5) if max_change_fraction is not None else None),
              (float(target_improvement_percent) if target_improvement_percent is not None else None),
              tuple(costs.items()),
              'v6-no-percent-cap-soft-stability-anchored-original')
    if (values[0] < 30 or not 0 < values[1] <= 1 or values[5] < 0
            or priority not in ('policy_first', 'balanced', 'guarded_fairness')
            or (values[6] is not None and not 0 < values[6] <= 1)):
        raise ValueError('Invalid adaptive fairness settings')
    return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()


@contextmanager
def _db(path=None):
    file = Path(path) if path is not None else db_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(file, timeout=15)
    try:
        con.row_factory = sqlite3.Row
        con.execute('''CREATE TABLE IF NOT EXISTS fairness_alns_trials (
            trial_id TEXT PRIMARY KEY, dataset_hash TEXT NOT NULL,
            context_key TEXT NOT NULL, run_id TEXT NOT NULL,
            round_number INTEGER NOT NULL, operator TEXT NOT NULL,
            reward REAL NOT NULL CHECK(reward >= 0 AND reward <= 1),
            accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
            previous_score REAL NOT NULL, candidate_score REAL,
            elapsed_seconds REAL NOT NULL, changed_sessions INTEGER,
            reason TEXT NOT NULL, created_at TEXT NOT NULL)''')
        con.execute('''CREATE INDEX IF NOT EXISTS fairness_alns_scope
            ON fairness_alns_trials(dataset_hash,context_key,operator)''')
        with con:
            yield con
    finally:
        con.close()


def operator_statistics(dataset_hash, context_key, operators=None, db_file=None):
    allowed = tuple(OPERATORS if operators is None else operators)
    if not allowed or any(op not in OPERATORS for op in allowed):
        raise ValueError('Unknown adaptive search operator')
    with _db(db_file) as con:
        rows = con.execute('''SELECT operator, COUNT(*) AS trials,
            AVG(reward) AS mean_reward, SUM(accepted) AS accepted
            FROM fairness_alns_trials WHERE dataset_hash=? AND context_key=?
            GROUP BY operator''', (dataset_hash, context_key)).fetchall()
    lookup = {row['operator']: row for row in rows}
    return [dict(operator=op, label=OPERATORS[op],
                 trials=int(lookup[op]['trials']) if op in lookup else 0,
                 mean_reward=round(float(lookup[op]['mean_reward']), 6) if op in lookup else 0.,
                 accepted=int(lookup[op]['accepted']) if op in lookup else 0)
            for op in allowed]


def choose_operator(dataset_hash, context_key, operators=None, db_file=None):
    stats = operator_statistics(dataset_hash, context_key, operators, db_file)
    untried = next((x for x in stats if x['trials'] == 0), None)
    if untried is not None:
        return dict(operator=untried['operator'], mode='explore',
                    reason='This neighborhood strategy has not been tested yet.', statistics=stats)
    total = sum(x['trials'] for x in stats)
    choice = max(stats, key=lambda x: x['mean_reward'] +
                 math.sqrt(2 * math.log(max(2, total)) / x['trials']))
    return dict(operator=choice['operator'], mode='ucb1',
                reason='Selected using observed rewards and the UCB1 exploration bonus.',
                statistics=stats)


def _quality(result):
    if (result.get('status_code') != 0 or result.get('independent_status') not in VALID
            or 'timetable_all.csv' not in result.get('files', {})
            or 'quality_summary.json' not in result.get('files', {})):
        raise ValueError('No independently validated timetable and quality report')
    report = json.loads(result['files']['quality_summary.json'])
    if not isinstance(report, dict) or not isinstance(report.get('metrics'), dict):
        raise ValueError('Missing measured fairness metrics')
    required = ('teacher_gap_periods', 'section_avoidable_gap_proxy',
                'teacher_daily_overload_periods', 'total_teacher_weekday_load_spread',
                'preferred_free_slot_periods', 'late_session_periods')
    if not all(k in report['metrics'] for k in required):
        raise ValueError('Incomplete independent quality measurements')
    return report


def _placements(csv_bytes):
    import csv
    import io
    rows = csv.DictReader(io.StringIO(csv_bytes.decode('utf-8-sig')))
    result = {}
    for row in rows:
        sid = row.get('session_id')
        if not sid:
            raise ValueError('Missing session ID')
        result.setdefault(sid, []).append((row.get('day'), row.get('period'), row.get('room_id')))
    return {sid: tuple(sorted(parts)) for sid, parts in result.items()}



def final_placement_audit(baseline_csv: bytes, final_csv: bytes, *, change_costs=None) -> tuple[dict, list[dict]]:
    """Count *distinct session IDs* changed between Standard and final Fairness.

    A two-period practical remains one session. A session moved in several ALNS
    rounds is counted only if its FINAL placement differs from Standard. A
    room-only reassignment is reported separately from a change of period/day.
    This is a placement audit, not a comparison of different optimizers.
    """
    def read(csv_bytes):
        reader = csv.DictReader(io.StringIO(csv_bytes.decode('utf-8-sig')))
        if not reader.fieldnames or not {'session_id', 'day', 'period', 'room_id'} <= set(reader.fieldnames):
            raise ValueError('Session placement CSV lacks required columns')
        sessions = {}
        for row in reader:
            sid = str(row.get('session_id') or '').strip()
            day = str(row.get('day') or '').strip().upper()
            period = str(row.get('period') or '').strip()
            room = str(row.get('room_id') or '').strip()
            if not all((sid, day, period, room)):
                raise ValueError('Incomplete scheduled placement')
            try:
                period = str(int(period))
            except ValueError as exc:
                raise ValueError('Invalid scheduled period') from exc
            entry = sessions.setdefault(sid, dict(
                slots=[], course_id=str(row.get('course_id') or ''),
                group_id=str(row.get('group_id') or ''),
                faculty_id=str(row.get('faculty_id') or ''),
                session_kind=str(row.get('kind') or 'THEORY').strip().upper()))
            entry['slots'].append((day, period, room))
        if not sessions:
            raise ValueError('No timetable sessions to audit')
        for entry in sessions.values():
            if len(set(entry['slots'])) != len(entry['slots']):
                raise ValueError('Repeated session placement')
            entry['slots'].sort()
        return sessions

    costs = validate_change_costs(change_costs)
    before, after = read(baseline_csv), read(final_csv)
    if before.keys() != after.keys():
        raise ValueError('The Standard and final timetable have different session coverage')
    changes = []
    time_moved = room_only = both_moved = 0
    for sid in sorted(before):
        old, new = before[sid], after[sid]
        if old['slots'] == new['slots']:
            continue
        old_times = sorted((day, period) for day, period, _ in old['slots'])
        new_times = sorted((day, period) for day, period, _ in new['slots'])
        old_rooms = sorted(room for _, _, room in old['slots'])
        new_rooms = sorted(room for _, _, room in new['slots'])
        time_changed = old_times != new_times
        room_changed = old_rooms != new_rooms
        if time_changed and room_changed:
            kind = 'time_and_room'
            both_moved += 1
        elif time_changed:
            kind = 'time_only'
        else:
            kind = 'room_only'
            room_only += 1
        if time_changed:
            time_moved += 1
        change = dict(session_id=sid,
                      course_id=old['course_id'], group_id=old['group_id'],
                      faculty_id=old['faculty_id'], session_kind=old['session_kind'],
                      change_type=kind,
                      standard_placement='; '.join(f'{d} P{p} / {r}' for d, p, r in old['slots']),
                      final_placement='; '.join(f'{d} P{p} / {r}' for d, p, r in new['slots']))
        change['weighted_cost'] = _change_cost(change, costs)
        changes.append(change)
    total = len(before)
    count = len(changes)
    return (dict(total_sessions=total, distinct_changed_sessions=count,
                 percent_changed_sessions=round(100 * count / total, 2),
                 unchanged_sessions=total-count, time_changed_sessions=time_moved,
                 room_only_changed_sessions=room_only,
                 time_and_room_changed_sessions=both_moved,
                 weighted_disruption_cost=sum(change['weighted_cost'] for change in changes),
                 practical_sessions_changed=sum(c['session_kind'] == 'PRACTICAL' for c in changes),
                 change_cost_weights=costs), changes)


def final_change_csv(changes: list[dict]) -> bytes:
    """Create a downloadable final-versus-original placement audit."""
    fields = ('session_id', 'course_id', 'group_id', 'faculty_id', 'session_kind',
              'change_type', 'weighted_cost', 'standard_placement', 'final_placement')
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(changes)
    return stream.getvalue().encode('utf-8-sig')


def max_changed_sessions(total_sessions: int, fraction: float) -> int:
    """Maximum *distinct session IDs* changed from Standard (floor, no rounding up).

    A double-period lab occupies two CSV rows but remains ONE session.
    A tiny timetable may have a zero-session cap; we never silently exceed it.
    """
    if not isinstance(total_sessions, int) or total_sessions < 1 or not 0 < float(fraction) <= 1:
        raise ValueError('Invalid baseline session count or change fraction')
    return math.floor(total_sessions * float(fraction) + 1e-9)


def _failure_detail(result):
    # Extract an actionable solver reason for rejected attempts; never claim success.
    if not isinstance(result, dict):
        return 'Solver did not return a candidate'
    logs = str(result.get('logs') or '')
    phrases = ('CP-SAT status:', 'Room assignment status', 'REJECTED baseline',
               'REJECTED Standard', 'No eligible room', 'More session times changed',
               'could not start solver', 'solver exceeded the execution time',
               'Independent validation did not complete')
    matching = [line.strip() for line in logs.splitlines()
                if any(needle.lower() in line.lower() for needle in phrases)]
    if matching:
        return matching[-1][:250]
    return f"Solver exit code {result.get('status_code', 'unknown')}; validation {result.get('independent_status', 'unknown')}"


def assess_candidate(best, candidate, profile, caps, *, standard_csv=None, max_change_fraction=None,
                     target_balanced_score=None, change_costs=None):
    """Never accept invalid, regressing, or overly disruptive candidates."""
    current = _quality(best)
    current_profile = fairness_score(current, profile)
    current_balanced = fairness_score(current, 'balanced')
    try:
        new = _quality(candidate)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False, 'No independently validated result: ' + _failure_detail(candidate), None, 0
    new_profile = fairness_score(new, profile)
    new_balanced = fairness_score(new, 'balanced')
    if (caps and (int(new['metrics']['preferred_free_slot_periods']) > int(caps['avoid'])
                  or int(new['metrics']['late_session_periods']) > int(caps['late']))):
        return False, 'Exceeded baseline soft-policy or late-period guardrail', new_balanced, 0
    # Before the target: improve fairness. Once the target has been reached:
    # prioritize reducing disruption while staying at/above the target.
    minimizing_disruption = (target_balanced_score is not None
                             and current_balanced <= target_balanced_score)
    if not minimizing_disruption:
        if new_profile >= current_profile:
            return False, 'Selected fairness objective did not improve', new_balanced, 0
        if new_balanced > current_balanced:
            return False, 'Balanced fairness score would regress', new_balanced, 0
    elif new_balanced > target_balanced_score:
        return False, 'Would lose the achieved fairness target', new_balanced, 0
    try:
        before = _placements(best['files']['timetable_all.csv'])
        after = _placements(candidate['files']['timetable_all.csv'])
        if before.keys() != after.keys():
            return False, 'Session coverage differs from validated incumbent', new_balanced, 0
        changed = sum(before[sid] != after[sid] for sid in before)
    except (ValueError, UnicodeDecodeError, KeyError):
        return False, 'Unable to compare scheduled placements', new_balanced, 0
    if not changed:
        return False, 'No scheduled sessions changed', new_balanced, 0
    if max_change_fraction is not None or minimizing_disruption or standard_csv is not None:
        if not isinstance(standard_csv, bytes):
            return False, 'Original Standard timetable is required for change-cap audit', new_balanced, 0
        try:
            net, _ = final_placement_audit(standard_csv, candidate['files']['timetable_all.csv'],
                                          change_costs=change_costs)
            allowance = (max_changed_sessions(net['total_sessions'], max_change_fraction)
                         if max_change_fraction is not None else net['total_sessions'])
        except (ValueError, UnicodeDecodeError, KeyError):
            return False, 'Cannot verify cumulative changes against Standard', new_balanced, 0
        if net['distinct_changed_sessions'] > allowance:
            return False, (f'Exceeded cumulative Standard change limit: '
                           f"{net['distinct_changed_sessions']} > {allowance} sessions"), new_balanced, 0
        if minimizing_disruption:
            previous, _ = final_placement_audit(standard_csv, best['files']['timetable_all.csv'],
                                                change_costs=change_costs)
            cheaper = net['weighted_disruption_cost'] < previous['weighted_disruption_cost']
            tied_but_better = (net['weighted_disruption_cost'] == previous['weighted_disruption_cost']
                               and new_balanced < current_balanced)
            if not (cheaper or tied_but_better):
                return False, 'Fairness target met, but candidate does not reduce disruption', new_balanced, 0
    return True, ('Reduced disruption while keeping the fairness target' if minimizing_disruption
                  else 'Validated, measurable improvement within change limit'), new_balanced, changed


def record_round(*, dataset_hash, context_key, run_id, round_number, operator,
                 reward, accepted, previous_score, candidate_score, elapsed_seconds,
                 changed_sessions, reason, db_file=None):
    if operator not in OPERATORS or not 0 <= reward <= 1:
        raise ValueError('Invalid neighborhood outcome')
    with _db(db_file) as con:
        con.execute('''INSERT INTO fairness_alns_trials
            (trial_id,dataset_hash,context_key,run_id,round_number,operator,reward,
             accepted,previous_score,candidate_score,elapsed_seconds,changed_sessions,
             reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (str(uuid.uuid4()), dataset_hash, context_key, run_id, round_number,
             operator, reward, int(accepted), previous_score, candidate_score,
             max(0., elapsed_seconds), changed_sessions, reason,
             datetime.now(timezone.utc).isoformat(timespec='seconds')))


def learning_history(dataset_hash, context_key, db_file=None):
    with _db(db_file) as con:
        rows = con.execute('''SELECT created_at,run_id,round_number,operator,
            previous_score,candidate_score,reward,accepted,elapsed_seconds,
            changed_sessions,reason FROM fairness_alns_trials
            WHERE dataset_hash=? AND context_key=? ORDER BY rowid''',
            (dataset_hash, context_key)).fetchall()
    return [dict(row) for row in rows]


def run_adaptive_search(*, baseline, solve, dataset_hash, context_key, profile,
                        caps, seconds, operators=None, max_rounds=4, db_file=None,
                        max_change_fraction=None, target_improvement_percent=None,
                        change_costs=None, clock=time.monotonic):
    """Run several bounded CP-SAT neighborhoods WITHOUT rerunning Standard.

    solve(operator, incumbent, round_index, remaining_solver_seconds) is the
    existing full solver+independent-validator pathway. When a candidate is
    rejected, the verified incumbent remains unchanged. A wall-clock budget
    controls new attempts; each individual solve has its own timeout.
    """
    valid = _quality(baseline)
    weights_for_profile(profile)
    cost_weights = validate_change_costs(change_costs)
    if target_improvement_percent is not None and not 0 < float(target_improvement_percent) <= 100:
        raise ValueError('Fairness improvement target must be within (0, 100]')
    allowed = tuple(OPERATORS if operators is None else operators)
    if not allowed or any(op not in OPERATORS for op in allowed):
        raise ValueError('Invalid adaptive neighborhood operators')
    if not 30 <= int(seconds) <= 3600 or not 1 <= int(max_rounds) <= 20:
        raise ValueError('Invalid adaptive fairness budget')
    start = clock()
    run_id = str(uuid.uuid4())
    best = baseline
    rounds = []
    initial_score = fairness_score(valid, 'balanced')
    original_csv = baseline['files']['timetable_all.csv']
    target_score = (initial_score * (1 - float(target_improvement_percent)/100)
                    if target_improvement_percent is not None else None)
    initial_audit, _ = final_placement_audit(original_csv, original_csv,
                                             change_costs=cost_weights)
    observed_tradeoffs = [dict(round=0, balanced_score=initial_score,
                               improvement_percent=0., distinct_changed_sessions=0,
                               weighted_disruption_cost=0, gain_since_previous_pp=0.,
                               added_weighted_cost=0, gain_per_extra_cost=None)]
    change_budget = (max_changed_sessions(initial_audit['total_sessions'], max_change_fraction)
                     if max_change_fraction is not None else None)
    for number in range(1, max_rounds + 1):
        remaining = seconds - (clock() - start)
        if remaining < 12:
            break
        preference = choose_operator(dataset_hash, context_key, allowed, db_file)
        operator = preference['operator']
        # Reserve time for model construction, room allocation and validation.
        portion = max(8, min(45, int(remaining / (max_rounds - number + 1)) - 8))
        before = fairness_score(_quality(best), 'balanced')
        old_audit, _ = final_placement_audit(original_csv, best['files']['timetable_all.csv'],
                                              change_costs=cost_weights)
        t0 = clock()
        try:
            candidate = solve(operator, best, number, portion)
            accepted, reason, candidate_score, changed = assess_candidate(
                best, candidate, profile, caps, standard_csv=original_csv,
                max_change_fraction=max_change_fraction,
                target_balanced_score=target_score, change_costs=cost_weights)
        except Exception as exc:
            accepted, reason, candidate_score, changed = False, 'Attempt error: ' + type(exc).__name__, None, 0
            candidate = None
        elapsed = max(0., clock() - t0)
        met_before = target_score is not None and before <= target_score
        if accepted:
            best = candidate
        improvement = max(0., before - candidate_score) if accepted else 0.
        net_audit, _ = final_placement_audit(original_csv, best['files']['timetable_all.csv'],
                                              change_costs=cost_weights)
        net_changed = net_audit['distinct_changed_sessions']
        net_cost = net_audit['weighted_disruption_cost']
        # Before the goal, UCB1 learns from *fairness gain per search time*.
        # The hard Standard-change cap already protects timetable stability.
        # The active target-free UCB1 uses measured gain, elapsed time and
        # extra disruption to learn productive REPAIR STRATEGIES. This is a
        # preference, not a rule that rejects independently valid improvements.
        time_factor = min(1., 30. / max(1., elapsed))
        if target_score is None:
            new_cost = max(0, net_cost - old_audit['weighted_disruption_cost'])
            stability_factor = 1. / (1. + new_cost / max(1., initial_audit['total_sessions'] * 0.15))
            reward = (improvement / max(1., before)) * time_factor * stability_factor
        elif accepted and met_before:
            reward = (max(0., old_audit['weighted_disruption_cost'] - net_cost)
                      / max(1., old_audit['weighted_disruption_cost'])) * time_factor
        else:
            reward = (improvement / max(1., before)) * time_factor
        reward = max(0., min(1., reward))
        if accepted:
            gain_pp = 100 * (before - candidate_score) / max(1., initial_score)
            cost_increment = net_cost - old_audit['weighted_disruption_cost']
            observed_tradeoffs.append(dict(round=number, balanced_score=candidate_score,
                improvement_percent=round(100*(initial_score-candidate_score)/max(1.,initial_score), 2),
                distinct_changed_sessions=net_changed, weighted_disruption_cost=net_cost,
                gain_since_previous_pp=round(gain_pp, 2), added_weighted_cost=cost_increment,
                gain_per_extra_cost=(round(gain_pp/cost_increment, 4)
                                     if cost_increment > 0 else None)))
        observation = dict(round=number, operator=operator, label=OPERATORS[operator],
                           phase=('minimum_disruption' if met_before else ('fairness_and_stability' if target_score is None else 'fairness_first')),
                           mode=preference['mode'], accepted=accepted, reward=round(reward, 6),
                           previous_score=before, candidate_score=candidate_score,
                           elapsed_seconds=round(elapsed, 2), changed_sessions=changed,
                           net_changed_from_standard=net_changed if accepted else None,
                           weighted_disruption_cost=net_cost if accepted else None,
                           target_reached=(candidate_score <= target_score if accepted and target_score is not None else None),
                           change_limit=change_budget,
                           neighborhood_fraction=(candidate.get('neighborhood_fraction') if candidate else None),
                           solver_exit_code=(candidate.get('status_code') if candidate else None),
                           independent_status=(candidate.get('independent_status') if candidate else 'NOT_RUN'),
                           reason=reason)
        rounds.append(observation)
        record_round(dataset_hash=dataset_hash, context_key=context_key,
                     run_id=run_id, round_number=number, operator=operator,
                     reward=reward, accepted=accepted, previous_score=before,
                     candidate_score=candidate_score, elapsed_seconds=elapsed,
                     changed_sessions=changed, reason=reason, db_file=db_file)
    final_score = fairness_score(_quality(best), 'balanced')
    change_audit, final_changes = final_placement_audit(
        baseline['files']['timetable_all.csv'], best['files']['timetable_all.csv'],
        change_costs=cost_weights)
    # Show only measured, accepted candidates from this run. No hypothetical
    # results or additional solver runs are required for this trade-off view.
    pareto = [point for point in observed_tradeoffs if not any(
        (other['balanced_score'] <= point['balanced_score']
         and other['weighted_disruption_cost'] <= point['weighted_disruption_cost'])
        and (other['balanced_score'] < point['balanced_score']
             or other['weighted_disruption_cost'] < point['weighted_disruption_cost'])
        for other in observed_tradeoffs)]
    target_met = (target_score is not None and final_score <= target_score)
    if target_score is None:
        target_message = ('No preset improvement target or change-percentage cap. '
                          'UCB1 seeks fairness gains with a preference for lower disruption.')
    elif target_met:
        target_message = ('Target reached; the retained result is the least disruptive '
                          'validated candidate found after the target was reached, not a global optimum.')
    else:
        target_message = ('Fairness target not reached within the search budget. '
                          'Best validated partial improvement retained.')
    if change_budget is not None and change_audit['distinct_changed_sessions'] > change_budget:
        raise ValueError('Final timetable exceeded cumulative change limit')
    search_text = (
        'UCB1 explores actual teaching-session neighborhoods. CP-SAT optimizes '
        'fairness with a soft cost for moving original Standard classes and rooms. '
        'There is no preset percent improvement goal or total-change cap. '
        'Only independently validated improvements that protect Standard policy limits are kept.'
        if target_score is None else
        'Legacy research protocol: before target, prioritize fairness without an additional '
        'soft time-move penalty; after the target, prefer lower disruption within the cap.'
    )
    summary = dict(version=('UCB1-ALNS-v6-soft-stability' if target_score is None
                            else 'UCB1-ALNS-v5-guarded-fairness'), run_id=run_id,
                   search_phases=search_text,
                   target_improvement_percent=target_improvement_percent,
                   target_balanced_score=target_score, target_reached=target_met,
                   target_message=target_message, change_cost_weights=cost_weights,
                   observed_tradeoffs=observed_tradeoffs, pareto_tradeoffs=pareto,
                   max_change_fraction=max_change_fraction,
                   max_changed_sessions=change_budget,
                   budget_seconds=int(seconds), elapsed_seconds=round(clock()-start, 2),
                   attempted=len(rounds), improved=sum(int(r['accepted']) for r in rounds),
                   starting_balanced_score=initial_score, best_balanced_score=final_score,
                   relative_improvement=round((initial_score - final_score)/max(1., initial_score), 6),
                   accepted_candidate=best is not baseline,
                   final_change_audit=change_audit, final_changed_sessions=final_changes,
                   final_validation_status=best.get('independent_status', 'NOT_RUN'),
                   rounds=rounds)
    return best, summary
