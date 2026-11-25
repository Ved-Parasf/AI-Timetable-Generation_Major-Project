"""Local online UCB1 fairness learning, independent of Groq.

Each trial is a *real* Fairness request from an independently validated Standard
baseline. The reward measures improvement on the SAME balanced fairness metric
for every focus, so scores are comparable; failure/fallback earns zero.

This module never edits Excel, constraint weights, solver state, or schedules.
It recommends one of the existing bounded fairness profiles for coordinator review.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from timetable_fairness import FAIRNESS_PROFILE_LABELS, fairness_score
from timetable_records import db_path

VALID_STATES = frozenset(('PASS', 'PASS_WITH_LIMITATIONS'))


def configuration_key(*, seconds: int, neighborhood_fraction: float,
                      priority: str, include_mentor: bool, extra_labs: int) -> str:
    """Separate experiments with different budgets/constraints to avoid comparisons."""
    secs = int(seconds)
    fraction = round(float(neighborhood_fraction), 5)
    labs = int(extra_labs)
    if not 30 <= secs <= 3600 or not 0 < fraction <= 1 or labs < 0:
        raise ValueError('Invalid fairness experiment settings')
    if priority not in ('policy_first', 'balanced'):
        raise ValueError('Invalid fairness priority')
    data = [secs, fraction, priority, bool(include_mentor), labs]
    return hashlib.sha256(json.dumps(data, separators=(',', ':')).encode()).hexdigest()


@contextmanager
def _connection(path=None):
    file = Path(path) if path else db_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(file, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('''CREATE TABLE IF NOT EXISTS fairness_learning_trials (
        trial_id TEXT PRIMARY KEY,
        dataset_hash TEXT NOT NULL,
        configuration_key TEXT NOT NULL,
        focus TEXT NOT NULL,
        baseline_score REAL NOT NULL,
        candidate_score REAL,
        reward REAL NOT NULL CHECK(reward >= 0 AND reward <= 1),
        accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
        created_at TEXT NOT NULL
    )''')
    con.execute('''CREATE INDEX IF NOT EXISTS fairness_learning_scope
        ON fairness_learning_trials(dataset_hash, configuration_key, focus)''')
    try:
        with con:
            yield con
    finally:
        con.close()


def _score(report):
    if not isinstance(report, dict) or not isinstance(report.get('metrics'), dict):
        raise ValueError('Missing independently measured fairness metrics')
    needed = ('teacher_gap_periods', 'section_avoidable_gap_proxy',
              'teacher_daily_overload_periods', 'total_teacher_weekday_load_spread')
    if not all(name in report['metrics'] for name in needed):
        raise ValueError('Incomplete fairness quality metrics')
    return float(fairness_score(report, 'balanced'))


def record_outcome(*, dataset_hash: str, config_key: str, focus: str,
                   standard_result: dict, published_result: dict,
                   db_file=None, trial_id=None) -> dict:
    """Record observed reward only after verifying the Standard baseline.

    A returned Standard fallback or failed fairness run counts as zero reward.
    Even if focus-specific fairness accepts a candidate, a worse aggregate
    balanced score gets zero reward. Thus UCB1 does not learn from LLM guesses.
    """
    if focus not in FAIRNESS_PROFILE_LABELS:
        raise ValueError('Unknown Fairness profile')
    if len(str(dataset_hash)) != 64 or len(str(config_key)) != 64:
        raise ValueError('Expected dataset/configuration SHA-256 identifiers')
    if (standard_result.get('status_code') != 0 or
            standard_result.get('independent_status') not in VALID_STATES):
        raise ValueError('Cannot learn without a validated Standard timetable')
    raw = standard_result.get('files', {}).get('quality_summary.json')
    if not raw:
        raise ValueError('Missing verified Standard quality report')
    baseline = _score(json.loads(raw))
    accepted = bool(published_result.get('status_code') == 0
                    and published_result.get('independent_status') in VALID_STATES
                    and published_result.get('fairness_enabled')
                    and not published_result.get('fell_back_to_standard', False))
    candidate_score = None
    reward = 0.0
    if accepted:
        finished = published_result.get('files', {}).get('quality_summary.json')
        if not finished:
            raise ValueError('Cannot learn from a Fairness result without quality metrics')
        candidate_score = _score(json.loads(finished))
        reward = max(0.0, min(1.0, (baseline - candidate_score) / max(1.0, baseline)))
    value = {'trial_id': str(trial_id or uuid.uuid4()), 'dataset_hash': dataset_hash,
             'configuration_key': config_key, 'focus': focus,
             'baseline_score': baseline, 'candidate_score': candidate_score,
             'reward': round(reward, 8), 'accepted': accepted}
    with _connection(db_file) as con:
        con.execute('''INSERT OR IGNORE INTO fairness_learning_trials
            (trial_id,dataset_hash,configuration_key,focus,baseline_score,
             candidate_score,reward,accepted,created_at)
             VALUES (?,?,?,?,?,?,?,?,?)''',
            (value['trial_id'], dataset_hash, config_key, focus, baseline,
             candidate_score, value['reward'], int(accepted),
             datetime.now(timezone.utc).isoformat(timespec='seconds')))
        con.commit()
    return value


def recommend_focus(*, dataset_hash: str, config_key: str,
                    allowed_focuses, db_file=None) -> dict:
    """UCB1: sample untried arms first, then maximize mean + exploration bonus.

    No learned preference is claimed before sufficient real trials exist.
    Allowed profiles come from the workbook's policy compatibility checks.
    """
    allowed = tuple(dict.fromkeys(allowed_focuses))
    if not allowed or any(f not in FAIRNESS_PROFILE_LABELS for f in allowed):
        raise ValueError('Unrecognized fairness profile options')
    with _connection(db_file) as con:
        rows = con.execute('''SELECT focus,COUNT(*) AS trials,AVG(reward) AS mean_reward,
             SUM(accepted) AS accepted_count
             FROM fairness_learning_trials WHERE dataset_hash=? AND configuration_key=?
             GROUP BY focus''', (dataset_hash, config_key)).fetchall()
    stats = {row['focus']: dict(trials=int(row['trials']),
                                mean_reward=float(row['mean_reward']),
                                accepted=int(row['accepted_count'])) for row in rows}
    total = sum(stats.get(f, {}).get('trials', 0) for f in allowed)
    untried = [f for f in allowed if stats.get(f, {}).get('trials', 0) == 0]
    if untried:
        selected = untried[0]
        reason = ('No experiments for this focus yet; UCB1 explores it before '
                  'comparing measured results.')
        mode = 'exploration'
    else:
        selected = max(allowed, key=lambda f: (
            stats[f]['mean_reward'] + math.sqrt(2 * math.log(max(2, total)) / stats[f]['trials']),
            -allowed.index(f)))
        reason = ('UCB1 used the observed improvement rewards and its '
                  'exploration bonus; this is not a guarantee of improvement.')
        mode = 'ucb1'
    return {'focus': selected, 'mode': mode, 'reason': reason,
            'total_trials': total,
            'stats': [dict(focus=f, label=FAIRNESS_PROFILE_LABELS[f],
                           trials=stats.get(f, {}).get('trials', 0),
                           mean_reward=round(stats.get(f, {}).get('mean_reward', 0.0), 5),
                           accepted=stats.get(f, {}).get('accepted', 0)) for f in allowed]}


def experiment_history(*, dataset_hash: str, config_key: str, db_file=None) -> list[dict]:
    """Export exact per-run evidence for the report/viva; read-only."""
    with _connection(db_file) as con:
        rows = con.execute('''SELECT created_at,focus,baseline_score,candidate_score,
            reward,accepted FROM fairness_learning_trials
            WHERE dataset_hash=? AND configuration_key=?
            ORDER BY created_at,trial_id''', (dataset_hash, config_key)).fetchall()
    return [dict(r) for r in rows]
