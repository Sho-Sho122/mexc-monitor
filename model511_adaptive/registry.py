"""Append-only research identities. No automatic hypothesis/threshold selection."""
import copy
from .config import base_model, canonical, digest, dt, iso, next_bucket
from .storage import audit, require_research_ready

OPS = {'eq', 'in', 'ge', 'le'}
RESERVED = {'gross_R', 'net_R', 'exit_result', 'exit_reference_price', 'result_r', 'mfe_pct', 'mae_pct'}
BASE_FIELDS = {'has_pullback', 'has_trend_volume', 'has_high_edge', 'has_oi_funding',
               'crowding_state', 'persistence_category', 'persistence_score', 'asset_class',
               'symbol', 'direction', 'strategy_count', 'strategy_set', 'funding_rate',
               'oi', 'oi_change_pct', 'majority_ratio', 'direction_imbalance'}


def normalize_model(spec, catalog=None):
    spec = copy.deepcopy(spec)
    if set(spec) != set(base_model()) or spec['universe'] != 'ALL_OBSERVED':
        raise ValueError('Explicit supported model schema/universe required')
    if spec['reference_policy'] != 'BETA1_BOUNDARIES' or spec['cost_policy'] != 'OBSERVED_EXPECTED_NOTIONAL':
        raise ValueError('Unsupported Reference/Execution policy')
    if not isinstance(spec['feature_schema_version'], str) or not spec['feature_schema_version'].isdigit() or int(spec['feature_schema_version']) < 1:
        raise ValueError('Invalid feature schema version')
    exit_ = spec['exit']
    if set(exit_) != {'sl_pct', 'rr', 'max_hours'} or any(isinstance(v, bool) for v in exit_.values()) or exit_['sl_pct'] not in (.25, .35, .50, .75, 1.) or exit_['rr'] not in (1., 1.5, 2.) or exit_['max_hours'] not in (3, 6, 12, 24):
        raise ValueError('Exit outside frozen 60-variant family')
    spec['exit'] = {'sl_pct': float(exit_['sl_pct']), 'rr': float(exit_['rr']), 'max_hours': int(exit_['max_hours'])}
    for condition in spec['entry']:
        if set(condition) != {'field', 'op', 'value'} or condition['op'] not in OPS:
            raise ValueError('Unsupported predicate')
        field = condition['field']
        if field in RESERVED or field not in BASE_FIELDS | set(catalog or {}):
            raise ValueError('Unavailable/outcome feature: ' + field)
        if condition['op'] in ('ge', 'le'):
            if isinstance(condition['value'], bool):
                raise ValueError('Numeric threshold cannot be boolean')
            condition['value'] = float(condition['value'])
        if field.startswith('has_') and (condition['op'] != 'eq' or not isinstance(condition['value'], bool)):
            raise ValueError('Boolean strategy membership requires an explicit boolean equality')
        if isinstance(condition['value'], (int, float)) and not isinstance(condition['value'], bool):
            condition['value'] = float(condition['value'])
            if condition['value'] == 0:
                condition['value'] = 0.0
        if condition['op'] == 'in':
            if not isinstance(condition['value'], list) or not condition['value']:
                raise ValueError('in requires a nonempty list')
            condition['value'] = sorted(set(0.0 if v == 0 else float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in condition['value']), key=canonical)
    spec['entry'] = sorted(spec['entry'], key=canonical)
    if not spec['entry'] or len({canonical(c) for c in spec['entry']}) != len(spec['entry']):
        raise ValueError('Empty/duplicate predicates')
    canonical(spec)
    return spec


def get(state, table, key, value):
    return next(r for r in state[table] if r[key] == value)


def trial(state, request, at):
    required = {'request_id', 'scan_id', 'hypothesis_family', 'features', 'parameters',
                'dataset_start_jst', 'dataset_end_jst', 'candidate_family_size', 'candidate_index',
                'result_summary', 'selected_for_registration', 'selection_reason', 'candidate_count_quality'}
    if not required.issubset(request):
        raise ValueError('Incomplete Trial Ledger entry: ' + str(required-set(request)))
    prior = next((t for t in state['trials'] if t['request_id'] == request['request_id']), None)
    if prior:
        if prior['request_hash'] != digest(request):
            raise ValueError('Trial request ID reused with different content')
        return prior
    scan = get(state, 'scans', 'scan_id', request['scan_id'])
    if not dt(request['dataset_start_jst']) <= dt(request['dataset_end_jst']) <= dt(scan['source_cutoff_jst']) <= dt(at):
        raise ValueError('Trial discovery/source cutoff mismatch')
    if request['candidate_count_quality'] not in ('EXACT', 'UNKNOWN'):
        raise ValueError('Unknown candidate count quality')
    size, index = request['candidate_family_size'], request['candidate_index']
    if not isinstance(size, int) or not isinstance(index, int) or not 1 <= index <= size:
        raise ValueError('Invalid candidate accounting')
    for t in state['trials']:
        if (t['scan_id'], t['hypothesis_family'], t['candidate_index']) == (request['scan_id'], request['hypothesis_family'], index):
            raise ValueError('Duplicate candidate index')
        if (t['scan_id'], t['hypothesis_family']) == (request['scan_id'], request['hypothesis_family']) and t['candidate_family_size'] != size:
            raise ValueError('Inconsistent family size')
    row = {**copy.deepcopy(request), 'trial_id': f'TRIAL-{len(state["trials"])+1:06d}',
           'request_hash': digest(request), 'created_at_jst': iso(at)}
    state['trials'].append(row)
    scan['candidate_count_examined_total'] = sum(1 for t in state['trials'] if t['scan_id'] == scan['scan_id'])
    audit(state, 'TRIAL_RECORDED', at, trial_id=row['trial_id'])
    return row


def register(state, request, at):
    required = {'request_id', 'trial_id', 'parent_model_id', 'hypothesis_family', 'hypothesis_description',
                'economic_or_market_rationale', 'model_spec_json', 'major_concept_change', 'threshold_rationale'}
    if not required.issubset(request) or not request['economic_or_market_rationale'] or not request['threshold_rationale']:
        raise ValueError('Hypothesis rationale/provenance required')
    prior = next((h for h in state['hypotheses'] if h['request_id'] == request['request_id']), None)
    if prior:
        if prior['request_hash'] != digest(request):
            raise ValueError('Hypothesis request ID reused')
        return prior
    t = get(state, 'trials', 'trial_id', request['trial_id'])
    require_research_ready(state)
    scan = get(state, 'scans', 'scan_id', t['scan_id'])
    if scan['data_quality_status'] == 'BLOCK_RESEARCH':
        raise ValueError('BLOCK_RESEARCH forbids new hypotheses')
    if dt(at) < dt(t['created_at_jst']):
        raise ValueError('Hypothesis predates trial')
    model = normalize_model(request['model_spec_json'], state['feature_catalog'])
    if int(model['feature_schema_version']) > int(state['feature_schema_version']):
        raise ValueError('Model uses an unregistered feature schema')
    if request['major_concept_change'] not in ('Persistence', 'Asset Class', 'Exit Family', 'Regime Feature', 'Universe Filter', 'Replication'):
        raise ValueError('One named major concept required')
    parent = base_model() if request['parent_model_id'] == 'BETA1' else get(state, 'challengers', 'challenger_id', request['parent_model_id'])['model_spec_json']
    changed = [k for k in model if k != 'feature_schema_version' and model[k] != normalize_model(parent, state['feature_catalog'])[k]]
    if len(changed) > 1:
        raise ValueError('Multiple major model changes require a separate specification')
    if 'entry' in changed:
        before = {canonical(c) for c in parent['entry']}
        after = {canonical(c) for c in model['entry']}
        fields = {c['field'] for c in model['entry'] + parent['entry'] if canonical(c) in before ^ after}
        if len(fields) > 1:
            raise ValueError('One major feature change per challenger')
    from .semantics import effective
    semantic = effective(model, state['feature_catalog'])
    identity = digest(semantic)
    duplicate = next((h for h in state['hypotheses'] if h['model_spec_hash'] == identity), None)
    row = {**copy.deepcopy(request), 'hypothesis_id': f'HYP-{len(state["hypotheses"])+1:06d}',
           'request_hash': digest(request), 'created_at_jst': iso(at), 'scan_id': t['scan_id'],
           'discovery_start_jst': t['dataset_start_jst'], 'discovery_end_jst': t['dataset_end_jst'],
           'source_cutoff_jst': scan['source_cutoff_jst'], 'model_spec_json': model, 'model_spec_hash': identity,
           'effective_model_spec': semantic,
           'features_used': [c['field'] for c in model['entry']], 'parameter_values': model,
           'candidate_count_examined': t['candidate_family_size'], 'candidate_count_quality': t['candidate_count_quality'],
           'multiple_testing_family_id': t['hypothesis_family'], 'discovery_summary': t['result_summary'],
           'status': 'DUPLICATE' if duplicate else 'REGISTERED',
           'duplicate_of': duplicate['hypothesis_id'] if duplicate else None, 'rejection_reason': None,
           'possible_duplicate_of': [h['hypothesis_id'] for h in state['hypotheses'] if h['hypothesis_family'] == request['hypothesis_family']]}
    state['hypotheses'].append(row)
    scan['new_hypothesis_count'] = sum(h['scan_id'] == scan['scan_id'] and h['status'] != 'DUPLICATE' for h in state['hypotheses'])
    audit(state, 'HYPOTHESIS_REGISTERED', at, hypothesis_id=row['hypothesis_id'], status=row['status'])
    return row


def freeze(state, hypothesis_id, at, commit):
    h = get(state, 'hypotheses', 'hypothesis_id', hypothesis_id)
    prior = next((c for c in state['challengers'] if c['hypothesis_id'] == hypothesis_id), None)
    if prior:
        return prior
    require_research_ready(state)
    if h['status'] != 'REGISTERED' or dt(at) < dt(h['created_at_jst']) or not commit:
        raise ValueError('Hypothesis not eligible for Freeze')
    if not h.get('effective_model_spec') or digest(h['effective_model_spec']) != h['model_spec_hash']:
        raise ValueError('Unresolved legacy hypothesis semantics; cannot Freeze')
    row = {'challenger_id': f'CH-{len(state["challengers"])+1:06d}', 'hypothesis_id': hypothesis_id,
           'parent_beta_id': h['parent_model_id'], 'created_at_jst': iso(at), 'forward_start_jst': iso(next_bucket(at)),
           'model_spec_json': copy.deepcopy(h['model_spec_json']), 'model_spec_hash': h['model_spec_hash'],
           'effective_model_spec': copy.deepcopy(h['effective_model_spec']),
           'feature_schema_version': h['model_spec_json']['feature_schema_version'], 'adaptive_spec_version': state['spec_version'],
           'spec_hash': state['spec_hash'], 'git_commit_hash': commit, 'status': 'FORWARD_TESTING',
           'evaluation_count': 0, 'gate_evaluation_history': [], 'first_evaluation_at_jst': None}
    from .semantics import model_blocks
    if model_blocks(row):
        raise ValueError('Frozen semantic snapshot does not match this decision engine')
    state['challengers'].append(row)
    h['status'] = 'FORWARD_TESTING'
    audit(state, 'CHALLENGER_FROZEN', at, challenger_id=row['challenger_id'], model_spec_hash=row['model_spec_hash'])
    return row


def reject(state, hypothesis_id, reason, at):
    h = get(state, 'hypotheses', 'hypothesis_id', hypothesis_id)
    if not reason or h['status'] not in ('REGISTERED', 'DISCOVERED', 'FORWARD_TESTING'):
        raise ValueError('Explicit reason required; promoted/duplicate identities cannot be rejected here')
    h.update(status='REJECTED', rejection_reason=reason)
    for c in state['challengers']:
        if c['hypothesis_id'] == hypothesis_id:
            c['status'] = 'REJECTED'  # preserve frozen specification and every observation
    audit(state, 'HYPOTHESIS_REJECTED', at, hypothesis_id=hypothesis_id, reason=reason)


def candidate_count(state):
    families = {}
    for t in state['trials']:
        if t['candidate_count_quality'] != 'EXACT':
            return None
        families.setdefault((t['scan_id'], t['hypothesis_family']), []).append(t)
    if any(len(v) != v[0]['candidate_family_size'] for v in families.values()):
        return None  # cannot hide unlogged losers
    return sum(len(v) for v in families.values())
