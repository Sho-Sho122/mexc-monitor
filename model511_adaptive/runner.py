"""Transactions for the three clocks, with snapshot provenance."""
import copy
from .config import dt, iso, digest
from .scheduler import due
from .analysis import scan_report
from .gate import evaluate_gate
from .final_confirmation import evaluate as evaluate_final
from .storage import audit, record_integrity


def redact_holdouts(state, source):
    """Do not feed a reserved Final period back to Discovery, including other models."""
    if not state['final_confirmations']:
        return source
    source = {**source, 'exit_tables': dict(source['exit_tables'])}
    if source['beta']:
        source['beta'] = dict(source['beta'])
    def held(t):
        return any(dt(f['final_start_jst']) <= dt(t) and (not f['final_decision_count'] or
                   dt(t) < dt(f['evaluation_cutoff_jst'])) for f in state['final_confirmations'])
    source['rows'] = [r for r in source['rows'] if not held(r['scan_time_jst'])]
    if source['beta']:
        source['beta']['events'] = [e for e in source['beta']['events'] if not held(e['entry_time_jst'])]
    from model511.config import iso as legacy_iso
    excluded = {t['trade_id'] for t in source['exit_tables']['research_exit_trades']
                if held(legacy_iso(t['scan_time_jst']))}
    for table in source['exit_tables']:
        source['exit_tables'][table] = [r for r in source['exit_tables'][table] if r['trade_id'] not in excluded]
    return source


def run(state, source, at, scans=True, gates=True, finals=True):
    state = copy.deepcopy(state)
    record_integrity(state, source, at)
    if source.get('beta') is not None and not any(i['level'] == 'BLOCK' for i in source['issues']):
        from .completeness import ingest
        if source['beta']['evaluation_mode'] != state['mode']:
            raise ValueError('Source mode mismatch')
        ingest(state, source['beta'], at)
    if state['last_scan_jst'] and dt(at) < dt(state['last_scan_jst']):
        raise ValueError('Backward scan time')
    manifest = {'snapshot_id': source['snapshot_id'], 'manifest': source['manifest'],
                'observed_at_jst': iso(at), 'source_commit': source['source_commit'],
                'source_schema_version': source['source_schema_version']}
    if not any(s['snapshot_id'] == manifest['snapshot_id'] for s in state['source_snapshots']):
        state['source_snapshots'].append(manifest)
    if scans:
        scan_source = redact_holdouts(state, source)
        for scheduled in due(state['anchor_jst'], at, 5, [s['scheduled_at_jst'] for s in state['scans']]):
            sid = f'SCAN_{dt(scheduled):%Y%m%d}_{len(state["scans"])+1:04d}'
            report = scan_report(state, scan_source, scheduled)
            status = 'BLOCK_RESEARCH' if any(i['level'] == 'BLOCK' for i in source['issues']) else 'WARN' if source['issues'] else 'PASS'
            row = {'scan_id': sid, 'scheduled_at_jst': scheduled, 'started_at_jst': iso(at),
                   'completed_at_jst': iso(at), 'source_cutoff_jst': scheduled,
                   'source_snapshot_id': source['snapshot_id'], 'git_commit_hash': source['source_commit'],
                   'adaptive_spec_version': state['spec_version'], 'feature_schema_version': state['feature_schema_version'],
                   'data_quality_status': status, 'new_hypothesis_count': 0,
                   'candidate_count_examined_total': 0, 'status': 'BLOCK_RESEARCH' if status == 'BLOCK_RESEARCH' else 'NO_NEW_HYPOTHESIS',
                   'report': report, 'mode': state['mode']}
            state['scans'].append(row)
            state['last_scan_jst'] = scheduled
            audit(state, 'SCAN_RECORDED', at, scan_id=sid, snapshot_id=source['snapshot_id'])
    if gates:
        for scheduled in due(state['anchor_jst'], at, 15, [g['scheduled_at_jst'] for g in state['gate_decisions']]):
            evaluate_gate(state, scheduled, at, source['issues'])
    if finals:
        for f in state['final_confirmations']:
            evaluate_final(state, f['final_id'], at, source['issues'])
    return state
