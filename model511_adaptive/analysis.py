"""Scan layers; missing metrics are explicit, not invented observations."""
from collections import defaultdict
from .statistics import summarize, robustness
from .source_reader import discovery_events, exit_analysis
from .config import dt, iso
import copy


def execution_summary(rows):
    fields = ('data_age_ms', 'exit_data_age_ms', 'exit_quote_delay_ms', 'spread_R', 'fee_R', 'slippage_R')
    out = {'cost_status_counts': {}, 'missing_cost_inputs_counts': {}}
    for r in rows:
        reason = r.get('cost_status', 'UNAVAILABLE')
        out['cost_status_counts'][reason] = out['cost_status_counts'].get(reason, 0)+1
        for k in r.get('missing_cost_inputs', []):
            out['missing_cost_inputs_counts'][k] = out['missing_cost_inputs_counts'].get(k, 0)+1
    for field in fields:
        vals = [r[field] for r in rows if isinstance(r.get(field), (int, float))]
        out[field] = {'n': len(vals), 'mean': sum(vals)/len(vals) if vals else None,
                      'min': min(vals) if vals else None, 'max': max(vals) if vals else None}
    return out


def scan_report(state, source, cutoff):
    events = discovery_events(source, cutoff)
    beta = source['beta'] or {}
    results = {r['event_id']: r for r in beta.get('results', [])
               if dt(r['exit_timestamp']) <= dt(cutoff) and dt(r.get('execution_decision_time', r['exit_timestamp'])) <= dt(cutoff)}
    beta_events = {e['event_id']: e for e in beta.get('events', []) if dt(e['source_cutoff_jst']) <= dt(cutoff)}
    # Do not attach a beta outcome to a different canonical reference entry.
    rows = []
    for e in events:
        ref = beta_events.get(e['event_id'])
        result = results.get(e['event_id'], {}) if ref and (ref['entry_price'], ref['entry_time_jst']) == (e['entry_price'], e['entry_time_jst']) else {}
        rows.append({**e, **result})
    strategy = {}
    for name in ('PULLBACK', 'TREND_VOLUME', 'OI_FUNDING', 'HIGH_EDGE'):
        subset = [r for r in rows if name in r['strategy_set'].split('|')]
        strategy[name] = {'raw_open_n': sum(r['strategy'] == name and dt(r['scan_time_jst']) <= dt(cutoff) for r in source['rows']),
                          **summarize(subset, False), 'slices': robustness(subset, state['anchor_jst']),
                          'outcome_basis': 'matching beta1 reference only; other outcomes unavailable'}
    overlap = defaultdict(list)
    for r in rows:
        overlap[r['strategy_set']].append(r)
    beta_models = {}
    for m in beta.get('memberships', []):
        if m.get('eligible') and m['event_id'] in beta_events:
            e = beta_events[m['event_id']]
            if dt(e['entry_time_jst']) < dt(cutoff):
                beta_models.setdefault(m['model_id'], []).append({**e, **results.get(m['event_id'], {})})
    adaptive_models = {}
    from .forward import cohort
    for c in state['challengers']:
        adaptive_models[c['challenger_id']] = summarize(cohort(state, c['challenger_id'], cutoff))
    return {'evidence_kind': 'DISCOVERY', 'source_cutoff_jst': iso(cutoff), 'data_quality': source['issues'],
            'four_strategy_summary': strategy,
            'cross_strategy_summary': {k: summarize(v, False) for k, v in sorted(overlap.items())},
            'regime_summary': robustness(rows, state['anchor_jst']), 'exit_robustness': exit_analysis(source, cutoff),
            'beta1_models': {k: summarize(v) for k, v in beta_models.items()},
            'adaptive_models': adaptive_models, 'execution_net_summary': execution_summary(list(results.values())),
            'hypotheses': copy.deepcopy(state['hypotheses']), 'candidate_count': len(state['trials']),
            'new_hypotheses': [], 'hypothesis_registration_status': 'NO_NEW_HYPOTHESIS',
            'edge_decay_status': 'INSUFFICIENT', 'edge_decay_reason': 'Live thresholds not fixed; Micro Live out of scope'}
