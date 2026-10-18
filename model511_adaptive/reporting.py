"""Read-only reports, keeping cumulative and post-promotion evidence distinct."""
from .config import iso
from .forward import cohort
from .statistics import summarize, robustness
from .registry import candidate_count
from .completeness import certification
from .semantics import model_blocks


def report(state, cutoff):
    models = []
    for c in state['challengers']:
        rows = cohort(state, c['challenger_id'], cutoff)
        b = next((b for b in state['betas'] if b['source_challenger_id'] == c['challenger_id']), None)
        models.append({'challenger_id': c['challenger_id'], 'model_spec_hash': c['model_spec_hash'],
                       'certification_blocks': certification(state, c, c['forward_start_jst'], cutoff, 'practice')[1],
                       'cumulative_forward': summarize(rows),
                       'post_beta_promotion': summarize(cohort(state, c['challenger_id'], cutoff, b['promoted_at_jst'])) if b else None,
                       'robustness': robustness(rows, c['forward_start_jst'])})
    finals = []
    for f in state['final_confirmations']:
        row = {k: v for k, v in f.items() if k != 'sealed_evidence'}
        c = next(c for c in state['challengers'] if c['challenger_id'] == f['challenger_id'])
        blocks = model_blocks(c)
        if blocks:
            row.update(historical_final_decision=f['final_decision'], final_decision=None,
                       status='UNRESOLVED', certification_blocks=blocks)
        finals.append(row)
    return {'spec_version': state['spec_version'], 'spec_hash': state['spec_hash'], 'mode': state['mode'],
            'as_of': iso(cutoff), 'models': models, 'scans': state['scans'], 'gates': state['gate_decisions'],
            'hypotheses': state['hypotheses'], 'candidate_count': candidate_count(state),
            'finals': finals,
            'audit': state['audit'], 'live_orders': False}
