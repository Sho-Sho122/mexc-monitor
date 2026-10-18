"""Independent state with OS lock, atomic commit, and rebuildable projections."""
import csv
import io
import json
from pathlib import Path
from model511.storage import atomic, lock
from .config import VERSION, SCHEMA, canonical, digest, dt, iso, spec_hash

TABLES = {'scans': 'research_scan.csv', 'hypotheses': 'hypothesis_registry.csv',
          'trials': 'trial_ledger.csv', 'challengers': 'challenger_registry.csv',
          'betas': 'beta_registry.csv', 'gate_decisions': 'gate_decisions.csv',
          'qualifications': 'qualification_history.csv', 'final_confirmations': 'final_confirmation.csv',
          'memberships': 'model_membership.csv', 'evidence': 'forward_evidence.csv'}


def empty(anchor, mode, commit):
    if mode not in ('FORWARD', 'BACKFILL') or not commit:
        raise ValueError('Explicit mode and source commit required')
    return {'schema_version': SCHEMA, 'spec_version': VERSION, 'spec_hash': spec_hash(),
            'mode': mode, 'anchor_jst': iso(anchor), 'git_commit_hash': commit,
            'feature_schema_version': '1', 'last_scan_jst': None, 'last_gate_jst': None,
            'last_update_jst': None, 'revision': 0, 'audit': [], 'source_snapshots': [],
            'feature_catalog': {}, 'seen_events': {}, 'source_history': {}, 'population_unresolved_ids': [],
            'execution_settings': {}, 'observations': {}, 'benchmarks': {},
            **{k: [] for k in TABLES}}


def audit(state, action, at, **details):
    row = {'action': action, 'at_jst': iso(at), **details}
    if row not in state['audit']:
        state['audit'].append(row)


def research_blocks(state):
    """One precondition for every operation that advances research.

    A historical PASS must not hide the latest blocked scan or live assessment.
    Missing assessments are not evidence that inputs are safe.
    """
    scans = state.get('scans', [])
    latest = scans[-1] if scans else None
    assessment = state.get('research_integrity')
    statuses = [s.get('data_quality_status') for s in (latest, assessment) if s]
    if not statuses or any(s not in ('PASS', 'WARN') for s in statuses):
        return ['BLOCK_RESEARCH']
    return []


def require_research_ready(state):
    if research_blocks(state):
        raise ValueError('BLOCK_RESEARCH: current source integrity does not permit this transition')


def record_integrity(state, source, at):
    issues = source['issues']
    state['research_integrity'] = {
        'assessed_at_jst': iso(at), 'source_snapshot_id': source['snapshot_id'],
        'data_quality_status': 'BLOCK_RESEARCH' if any(i['level'] == 'BLOCK' for i in issues) else 'WARN' if issues else 'PASS',
        'issues': issues,
    }


def validate(state):
    if state['schema_version'] != SCHEMA or state['spec_version'] != VERSION or state['spec_hash'] != spec_hash():
        raise ValueError('State schema/spec mismatch; explicit migration required')
    if state['mode'] not in ('FORWARD', 'BACKFILL') or not state['git_commit_hash']:
        raise ValueError('Invalid mode/provenance')
    dt(state['anchor_jst'])
    ids = {'scans': 'scan_id', 'hypotheses': 'hypothesis_id', 'trials': 'trial_id',
           'challengers': 'challenger_id', 'betas': 'beta_id', 'gate_decisions': 'gate_id',
           'final_confirmations': 'final_id', 'memberships': 'membership_id', 'evidence': 'evidence_id'}
    for table, key in ids.items():
        values = [r[key] for r in state[table]]
        if len(values) != len(set(values)):
            raise ValueError('Duplicate ID: ' + table)
    models = {c['challenger_id']: c for c in state['challengers']}
    for c in models.values():
        from .registry import normalize_model
        frozen_features = c.get('effective_model_spec', {}).get('features', {})
        if normalize_model(c['model_spec_json'], frozen_features) != c['model_spec_json']:
            raise ValueError('Noncanonical frozen model')
        from .semantics import model_blocks
        unresolved_legacy = not c.get('effective_model_spec') and bool(state.get('schema_migrations'))
        if (not unresolved_legacy and model_blocks(c)) or c['spec_hash'] != state['spec_hash']:
            raise ValueError('Model hash/spec mismatch')
        if not c['git_commit_hash'] or dt(c['forward_start_jst']) <= dt(c['created_at_jst']):
            raise ValueError('Invalid frozen provenance/Forward start')
    for row in state['evidence'] + state['memberships']:
        c = models[row['challenger_id']]
        if row['model_spec_hash'] != c['model_spec_hash'] or dt(row['entry_time_jst']) < dt(c['forward_start_jst']):
            raise ValueError('Forward/hash contamination')
        if row['mode'] != state['mode']:
            raise ValueError('BACKFILL contamination')
    for b in state['betas']:
        c = models[b['source_challenger_id']]
        if b['model_spec_hash'] != c['model_spec_hash'] or b['cumulative_forward_start_jst'] != c['forward_start_jst']:
            raise ValueError('Beta Forward reset/hash mismatch')
        g = next((g for g in state['gate_decisions'] if g['gate_id'] == b['gate_id']), None)
        if not g or g['decision'] != 'PROMOTE_TO_BETA' or not g['human_approval'] or g['human_approval']['challenger_id'] != c['challenger_id']:
            raise ValueError('Beta without human Gate approval')
    promoted = [b['gate_id'] for b in state['betas']]
    if len(promoted) != len(set(promoted)):
        raise ValueError('More than one beta per Gate')
    for f in state['final_confirmations']:
        if f['final_decision_count'] not in (0, 1):
            raise ValueError('Final repeated decision')
        c = models[f['challenger_id']]
        if f['model_spec_hash'] != c['model_spec_hash'] or f['frozen_max_hours'] != c['model_spec_json']['exit']['max_hours']:
            raise ValueError('Final model/Exit mismatch')
        cutoffs = [e['evaluation_cutoff_jst'] for e in f['evaluations']]
        if cutoffs != sorted(set(cutoffs)):
            raise ValueError('Repeated/backward Final evaluations')
        if f['final_decision_count'] and (f['final_decision'] not in ('PASS', 'FAIL') or digest(f['sealed_evidence']) != f['evaluations'][-1]['snapshot_hash']):
            raise ValueError('Final seal mismatch')
    canonical(state)
    return True


def read(directory):
    state = json.loads((Path(directory) / 'adaptive_state.json').read_text('utf-8'))
    from .completeness import migrate
    state = migrate(state)
    validate(state)
    return state


def export(directory, state):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for table, filename in TABLES.items():
        rows = state[table]
        fields = sorted({k for r in rows for k in r}) or ['id']
        buff = io.StringIO(newline='')
        writer = csv.DictWriter(buff, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: canonical(v) if isinstance(v, (list, dict)) else v for k, v in r.items()})
        atomic(directory / filename, buff.getvalue().encode('utf-8-sig'))
    atomic(directory / 'adaptive_audit.log', ('\n'.join(canonical(a) for a in state['audit'])+'\n').encode())


def write(directory, state):
    validate(state)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # Caller holds lock throughout read-modify-write. State commits before projections.
    atomic(directory / 'adaptive_state.json', canonical(state).encode())
    export(directory, state)
