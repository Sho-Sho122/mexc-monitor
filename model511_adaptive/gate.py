"""Automatic evidence eligibility; promotion always requires a human record."""
import copy
from .config import GATE, PRACTICE, FINAL, COST_KEYS, dt, iso, digest
from .statistics import summarize, robustness, holm, cluster_bootstrap
from .registry import get, candidate_count
from .forward import cohort, benchmark_cohort, settings_valid
from .storage import audit, require_research_ready, research_blocks
from .completeness import certification
from .semantics import model_blocks


def minimum(metrics, start, cutoff, policy):
    failures = []
    if (dt(cutoff)-dt(start)).total_seconds() < policy['days']*86400:
        failures.append('AGE')
    for field, threshold in [('resolved_unique_events', policy['n']), ('time_cluster_30m_n', policy['clusters']), ('active_days', policy['active_days'])]:
        if metrics[field] < threshold:
            failures.append(field)
    return failures


def evaluate_gate(state, scheduled, at, issues=()):
    prior = next((g for g in state['gate_decisions'] if g['scheduled_at_jst'] == iso(scheduled)), None)
    if prior:
        return prior
    gid = f'GATE-{len(state["gate_decisions"])+1:06d}'
    candidates, pvalues = [], {}
    for c in state['challengers']:
        if dt(c['created_at_jst']) > dt(scheduled) or c['status'] != 'FORWARD_TESTING':
            continue
        rows, completeness_blocks = certification(state, c, c['forward_start_jst'], scheduled, 'gate')
        metrics = summarize(rows)
        net_diagnostic = cluster_bootstrap([r for r in rows if r.get('gross_R') is not None and r.get('net_R') is not None])
        missing = minimum(metrics, c['forward_start_jst'], scheduled, GATE)
        blocks = completeness_blocks + [i['reason'] for i in issues if i['level'] == 'BLOCK']
        if model_blocks(c):
            blocks.append('MODEL_HASH_MISMATCH')
        parent = c['parent_beta_id']
        parent_rows = cohort(state, parent, scheduled, c['forward_start_jst']) if any(x['challenger_id'] == parent for x in state['challengers']) else benchmark_cohort(state, 'CORE_B1_PULLBACK_OPPOSITE', c['forward_start_jst'], scheduled)
        parent_metrics = summarize(parent_rows)
        controls = {}
        for control in sorted({r['benchmark_model_id'] for r in state['benchmarks'].values() if r['benchmark_model_id'].startswith('CTRL_')}):
            control_metrics = summarize(benchmark_cohort(state, control, c['forward_start_jst'], scheduled), False)
            controls[control] = {'metrics': control_metrics, 'net_ev_difference': metrics['net_ev']-control_metrics['net_ev']
                                if metrics['net_ev'] is not None and control_metrics['net_ev'] is not None else None}
        row = {'challenger_id': c['challenger_id'], 'model_spec_hash': c['model_spec_hash'],
               'metrics': metrics, 'minimum_missing': missing, 'explicit_blocks': blocks,
               'net_evaluable_bootstrap_diagnostic': net_diagnostic,
               'p_value_population': 'net-evaluable resolved subset; coverage reported; not official full-cohort Net EV',
               'forward_age_days': (dt(scheduled)-dt(c['forward_start_jst'])).total_seconds()/86400,
               'robustness': robustness(rows, c['forward_start_jst']),
               'parent_net_ev_difference': metrics['net_ev']-parent_metrics['net_ev'] if metrics['net_ev'] is not None and parent_metrics['net_ev'] is not None else None,
               'control_difference': controls, 'comparison_basis': 'same calendar Forward interval; descriptive, unpaired',
               'control_difference_reason': None if controls else 'No available frozen control evidence',
               'past_gate_ids': list(c['gate_evaluation_history'])}
        candidates.append(row)
        pvalues[c['challenger_id']] = net_diagnostic['p_positive']
    correction = holm(pvalues, candidate_count(state))
    for row in candidates:
        if correction['status'] == 'MULTIPLE_TESTING_UNRESOLVED':
            row['explicit_blocks'].append('MULTIPLE_TESTING_UNRESOLVED')
        row['adjusted_p'] = correction['adjusted'].get(row['challenger_id'])
        row['status'] = ('INSUFFICIENT_FORWARD' if row['minimum_missing'] else 'INVALID_DATA' if row['explicit_blocks']
                         else 'ELIGIBLE_FOR_PROMOTION_REVIEW')
        c = get(state, 'challengers', 'challenger_id', row['challenger_id'])
        c['evaluation_count'] += 1
        c['gate_evaluation_history'].append(gid)
        if c['first_evaluation_at_jst'] is None:
            c['first_evaluation_at_jst'] = iso(at)
    row = {'gate_id': gid, 'scheduled_at_jst': iso(scheduled), 'evaluated_at_jst': iso(at),
           'source_cutoff_jst': iso(scheduled), 'candidates': candidates, 'multiple_testing': correction,
           'decision': 'NO_CHANGE', 'human_approval': None, 'mode': state['mode'],
           'reason': 'Human selection required; EV ranking disabled'}
    state['gate_decisions'].append(row)
    state['last_gate_jst'] = iso(scheduled)
    audit(state, 'GATE_EVALUATED', at, gate_id=gid)
    return row


def promote(state, gate_id, challenger_id, approval, at):
    if state['mode'] != 'FORWARD':
        raise ValueError('BACKFILL cannot promote')
    if set(approval) != {'actor', 'reason', 'confirmed_human'} or not approval['actor'] or not approval['reason'] or approval['confirmed_human'] is not True:
        raise ValueError('Explicit human approval required')
    g = get(state, 'gate_decisions', 'gate_id', gate_id)
    if dt(at) < dt(g['evaluated_at_jst']):
        raise ValueError('Approval predates Gate')
    prior = next((b for b in state['betas'] if b['gate_id'] == gate_id), None)
    if prior:
        if prior['source_challenger_id'] != challenger_id or g['human_approval']['approval'] != approval:
            raise ValueError('One immutable human approval/model per Gate')
        return prior
    require_research_ready(state)
    if any(b['source_challenger_id'] == challenger_id for b in state['betas']):
        raise ValueError('Challenger already promoted')
    candidate = next(r for r in g['candidates'] if r['challenger_id'] == challenger_id)
    if candidate['status'] != 'ELIGIBLE_FOR_PROMOTION_REVIEW':
        raise ValueError('Cannot approve an ineligible candidate')
    c = get(state, 'challengers', 'challenger_id', challenger_id)
    if c['status'] != 'FORWARD_TESTING' or model_blocks(c) or c['model_spec_hash'] != candidate['model_spec_hash']:
        raise ValueError('Model changed after Gate')
    _, blocks = certification(state, c, c['forward_start_jst'], g['source_cutoff_jst'], 'gate')
    if blocks:
        raise ValueError('Promotion completeness blocked: '+','.join(blocks))
    b = {'beta_id': f'BETA-{len(state["betas"])+2:06d}', 'parent_beta_id': c['parent_beta_id'],
         'source_challenger_id': challenger_id, 'promoted_at_jst': iso(at), 'beta_only_start_jst': iso(at),
         'model_frozen_at_jst': c['created_at_jst'], 'cumulative_forward_start_jst': c['forward_start_jst'],
         'forward_start_jst': c['forward_start_jst'], 'model_spec_hash': c['model_spec_hash'],
         'git_commit_hash': c['git_commit_hash'], 'gate_id': gate_id, 'status': 'ACTIVE',
         'evidence_summary': copy.deepcopy(candidate['metrics'])}
    for previous in state['betas']:
        if previous['status'] == 'ACTIVE':
            previous['status'] = 'SHADOW'
    state['betas'].append(b)
    c['status'] = 'PROMOTED'
    g.update(decision='PROMOTE_TO_BETA', human_approval={'approval': copy.deepcopy(approval),
             'at_jst': iso(at), 'model_spec_hash': c['model_spec_hash'], 'challenger_id': challenger_id,
             'source_cutoff_jst': g['source_cutoff_jst']})
    get(state, 'hypotheses', 'hypothesis_id', c['hypothesis_id'])['status'] = 'PROMOTED'
    audit(state, 'HUMAN_PROMOTION', at, beta_id=b['beta_id'], gate_id=gate_id, actor=approval['actor'])
    return b


def practice(state, challenger_id, at):
    c = get(state, 'challengers', 'challenger_id', challenger_id)
    rows, blocks = certification(state, c, c['forward_start_jst'], at, 'practice')
    metrics = summarize(rows)
    blocks += minimum(metrics, c['created_at_jst'], at, PRACTICE)
    if state['mode'] != 'FORWARD':
        blocks.append('BACKFILL_NOT_FORWARD')
    if not any(b['source_challenger_id'] == challenger_id for b in state['betas']):
        blocks.append('NOT_PROMOTED_BETA')
    if not settings_valid(state['execution_settings']):
        blocks.append('EXECUTION_COST_CONFIG_BLOCKER')
    if not metrics['formal_net_complete']:
        blocks.append('NET_INCOMPLETE')
    ci = metrics['bootstrap_30m']['ci']
    if metrics['net_ev'] is None or metrics['net_ev'] <= 0 or ci is None or ci[0] <= 0:
        blocks.append('NET_EVIDENCE_INSUFFICIENT')
    return {'challenger_id': challenger_id, 'at_jst': iso(at), 'metrics': metrics,
            'status': 'PRACTICE_PREQUALIFIED' if not blocks else 'INSUFFICIENT_DATA', 'blocks': blocks}
