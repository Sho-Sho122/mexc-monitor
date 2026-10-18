"""Prerecorded daily cutoff; readiness extension, then exactly one decision."""
from .config import FINAL, BOOTSTRAP, dt, iso, digest
from .gate import minimum, practice
from .registry import get, candidate_count
from .forward import cohort, settings_valid
from .scheduler import daily_cutoff
from .statistics import summarize, robustness
from .storage import audit, require_research_ready, research_blocks
from datetime import timedelta
from .completeness import certification


def start(state, challenger_id, daily_time, at):
    require_research_ready(state)
    # Validate clock even when no scheduled evaluation is due yet.
    daily_cutoff(at, daily_time, at)
    q = practice(state, challenger_id, at)
    if q['blocks']:
        raise ValueError('Final start blocked: ' + ','.join(q['blocks']))
    if any(f['challenger_id'] == challenger_id for f in state['final_confirmations']):
        raise ValueError('Same frozen model cannot retry Final')
    if any(f['final_decision_count'] == 0 for f in state['final_confirmations']):
        raise ValueError('An independent Final holdout is already reserved')
    for f in state['final_confirmations']:
        if dt(at) <= dt(f['evaluation_cutoff_jst']):
            raise ValueError('Final periods cannot be reused')
    c = get(state, 'challengers', 'challenger_id', challenger_id)
    row = {'final_id': f'FINAL-{len(state["final_confirmations"])+1:06d}', 'challenger_id': challenger_id,
           'model_spec_hash': c['model_spec_hash'], 'final_start_jst': iso(at),
           'final_daily_evaluation_time_jst': daily_time, 'evaluations': [],
           'evaluation_cutoff_jst': None, 'final_decision_count': 0, 'final_decision': None,
           'status': 'FINAL_CONFIRMATION', 'bootstrap_settings': dict(BOOTSTRAP),
           'frozen_max_hours': c['model_spec_json']['exit']['max_hours'], 'prequalification': q}
    state['qualifications'].append(q)
    state['final_confirmations'].append(row)
    audit(state, 'FINAL_RESERVED', at, final_id=row['final_id'])
    return row


def evaluate(state, final_id, at, issues=(), _scheduled_cutoff=None):
    f = get(state, 'final_confirmations', 'final_id', final_id)
    if f['final_decision_count']:
        from .semantics import model_blocks
        if model_blocks(get(state, 'challengers', 'challenger_id', f['challenger_id'])):
            raise ValueError('Historical Final semantics unresolved; certification unavailable')
        return f  # sealed forever; even new evidence cannot change the decision
    cutoff = _scheduled_cutoff or daily_cutoff(f['final_start_jst'], f['final_daily_evaluation_time_jst'], at)
    if cutoff is None or any(e['evaluation_cutoff_jst'] == iso(cutoff) for e in f['evaluations']):
        return f
    if f['evaluations'] and cutoff < dt(f['evaluations'][-1]['evaluation_cutoff_jst']):
        raise ValueError('Final evaluation moved backwards')
    # A workflow outage must not silently skip an earlier ready scheduled cohort.
    # Replay missed scheduled cutoffs only against evidence available by that cutoff.
    previous = dt(f['evaluations'][-1]['evaluation_cutoff_jst']) if f['evaluations'] else daily_cutoff(
        dt(f['final_start_jst'])-timedelta(days=2), f['final_daily_evaluation_time_jst'], f['final_start_jst'])
    cursor = previous+timedelta(days=1)
    while cursor < cutoff:
        audit(state, 'FINAL_MISSED_SCHEDULE_REPLAY', at, final_id=final_id, scheduled_at_jst=iso(cursor), reason='WORKFLOW_DELAY_OR_OUTAGE')
        evaluate(state, final_id, at, issues, _scheduled_cutoff=cursor)
        if f['final_decision_count']:
            return f
        cursor += timedelta(days=1)
    c = get(state, 'challengers', 'challenger_id', f['challenger_id'])
    if c['model_spec_hash'] != f['model_spec_hash']:
        raise ValueError('Final frozen model mismatch')
    rows, completeness_blocks = certification(state, c, f['final_start_jst'], cutoff, 'final')
    # Do not calculate confidence/p-value before preparation requirements are met.
    metrics = summarize(rows, False)
    blocks = completeness_blocks + minimum(metrics, f['final_start_jst'], cutoff, FINAL)
    population_watermark = state.get('entry_population_complete_through_jst')
    if not population_watermark or dt(population_watermark) < cutoff:
        blocks.append('ENTRY_POPULATION_INCOMPLETE')
    if metrics['pending_reference_n']:
        blocks.append('PENDING_REFERENCE_TRADES')
    if not metrics['formal_net_complete']:
        blocks.append('NET_INCOMPLETE')
    if not settings_valid(state['execution_settings']):
        blocks.append('EXECUTION_COST_CONFIG_BLOCKER')
    blocks.extend(i['reason'] for i in issues if i['level'] == 'BLOCK')
    evaluation = {'evaluation_cutoff_jst': iso(cutoff), 'evaluated_at_jst': iso(at),
                  'evaluated_event_ids': sorted(r['event_id'] for r in rows),
                  'pending_reference_event_ids': sorted(r['event_id'] for r in rows if not r.get('exit_result')),
                  'snapshot_hash': digest(rows), 'blocks': blocks,
                  'entry_population_complete_through_jst': population_watermark,
                  'readiness': {k: metrics[k] for k in ('resolved_unique_events', 'time_cluster_30m_n', 'active_days', 'net_coverage_ratio', 'pending_reference_n')},
                  'status': 'FINAL_INSUFFICIENT_DATA'}
    f['evaluation_cutoff_jst'] = iso(cutoff)
    if not blocks:
        metrics = summarize(rows)
        ci = metrics['bootstrap_30m']['ci']
        passed = metrics['net_ev'] is not None and metrics['net_ev'] > 0 and ci is not None and ci[0] > 0
        f['final_decision'] = 'PASS' if passed else 'FAIL'
        f['final_decision_count'] = 1
        evaluation.update(status='FINAL_'+f['final_decision'], metrics=metrics,
                          robustness=robustness(rows, f['final_start_jst']))
        f['sealed_evidence'] = rows
        if passed:
            f['notification'] = {'type': 'PRACTICE_CANDIDATE_READY', 'model': c['challenger_id'],
                                 'model_hash': c['model_spec_hash'], 'frozen_at': c['created_at_jst'],
                                 'adaptive_spec_version': state['spec_version'], 'spec_hash': state['spec_hash'],
                                 'git_commit_hash': c['git_commit_hash'], 'final_confirmation': evaluation,
                                 'hypotheses_examined': candidate_count(state), 'challengers_tested': len(state['challengers']),
                                 'prequalification': f['prequalification'],
                                 'message': '事前固定した511 Practice Candidate認定基準を、未使用Forwardデータ上で通過したモデルが存在します。'}
            state['qualifications'].append(f['notification'])
    f['status'] = evaluation['status']
    f['evaluations'].append(evaluation)
    audit(state, 'FINAL_DAILY_EVALUATION', at, final_id=final_id, cutoff=iso(cutoff), status=f['status'])
    return f
