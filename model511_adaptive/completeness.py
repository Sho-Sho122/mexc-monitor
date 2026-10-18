"""Authoritative source population and common fail-closed certification proof."""
import copy
from datetime import timedelta
from .config import dt, iso, digest
from .semantics import membership, model_blocks


def ingest(state, beta, at):
    """Restore only hash-identical source bodies; never infer historical values."""
    from .storage import audit
    at = dt(at)
    history = state.setdefault('source_history', {})
    unresolved = set()
    recovered = []
    by_id = {}
    for event in beta['events']:
        eid = event['event_id']
        if eid in by_id and by_id[eid] != event:
            raise ValueError('Conflicting source Event ID')
        if event.get('evaluation_mode') != state['mode']:
            raise ValueError('Source mode mismatch')
        by_id[eid] = event
    for eid in sorted(set(by_id) | set(state['seen_events']) | set(history)):
        event = by_id.get(eid)
        prior = state['seen_events'].get(eid)
        old = history.get(eid)
        if event is None:
            unresolved.add(eid)  # truncated formal history is never empty population
            continue
        if dt(event['source_cutoff_jst']) > at or dt(event['entry_time_jst']) > at:
            unresolved.add(eid)
            continue
        if old and digest(old) != digest(event):
            unresolved.add(eid)
            continue
        if eid in state['seen_events'] and (not isinstance(prior, dict) or prior.get('hash') != digest(event)):
            unresolved.add(eid)
            continue
        if prior and not prior.get('event'):
            prior['event'] = copy.deepcopy(event)
            recovered.append(eid)
        history[eid] = copy.deepcopy(event)
    state['source_history'] = dict(sorted(history.items()))
    closed = beta.get('last_processed_bucket')
    end = dt(closed)+timedelta(minutes=5) if closed else None
    if end and end > at:
        raise ValueError('Future source coverage')
    state['entry_population_complete_through_jst'] = iso(end) if end else None
    if beta.get('forward_start_jst'):
        state['source_population_start_jst'] = beta['forward_start_jst']
    prior_unresolved = state.get('population_unresolved_ids', [])
    state['population_unresolved_ids'] = sorted(unresolved)
    if recovered or unresolved or prior_unresolved:
        record = {'recovered_ids': recovered, 'unresolved_ids': sorted(unresolved),
                  'source_identity': digest(by_id)}
        key = digest(record)
        if key not in state.setdefault('recovery_records', {}):
            state['recovery_records'][key] = record
            audit(state, 'SOURCE_RECOVERY', at, **record, restored_count=len(recovered))


def migrate(state):
    """Structural upgrade only. Missing legacy semantics remain unresolvable."""
    version = state.get('schema_version')
    if version not in (1, 2):
        raise ValueError('Unknown/future state schema; migration required')
    if version == 2:
        return state
    result = copy.deepcopy(state)
    result['schema_version'] = 2
    result['source_history'] = {}
    result['population_unresolved_ids'] = sorted(result['seen_events'])
    result['entry_population_complete_through_jst'] = None
    # No timestamp is invented, and no current Registry is used to repair models.
    result['schema_migrations'] = [{'from': 1, 'to': 2, 'source_state_hash': digest(state),
        'status': 'SOURCE_AND_SEMANTICS_VERIFICATION_REQUIRED'}]
    result['audit'].append({'action': 'SCHEMA_MIGRATION', 'from': 1, 'to': 2,
                           'source_state_hash': digest(state), 'restored_count': 0,
                           'missing_body_ids': sorted(k for k,v in result['seen_events'].items() if not isinstance(v,dict) or not v.get('event'))})
    return result


def population(state, c, start, cutoff, purpose):
    start, cutoff = dt(start), dt(cutoff)
    blocks = model_blocks(c)
    history = dict(state.get('source_history', {}))
    # First-observed immutable bodies are part of source history, even when
    # materialization was delayed. Missing bodies block, not silently disappear.
    for eid, observed in state['seen_events'].items():
        e = observed.get('event') if isinstance(observed, dict) else None
        if not e:
            blocks.append('SOURCE_BODY_MISSING')
        elif eid in history and history[eid] != e:
            blocks.append('SOURCE_HISTORY_CONFLICT')
        else:
            history[eid] = e
    if state.get('population_unresolved_ids'):
        blocks.append('SOURCE_HISTORY_INCOMPLETE')
    watermark = state.get('entry_population_complete_through_jst')
    if not watermark or dt(watermark) < cutoff:
        blocks.append('ENTRY_POPULATION_INCOMPLETE')
    beginning = state.get('source_population_start_jst')
    if not beginning or dt(beginning) > start:
        blocks.append('SOURCE_PERIOD_INCOMPLETE')
    evidence = {r['event_id']: r for r in state['evidence'] if r['challenger_id'] == c['challenger_id']}
    rows = []
    for eid, e in sorted(history.items()):
        if not start <= dt(e['entry_time_jst']) < cutoff:
            continue
        if purpose != 'final' and any(dt(f['final_start_jst']) <= dt(e['entry_time_jst']) and
             (not f['final_decision_count'] or dt(e['entry_time_jst']) < dt(f['evaluation_cutoff_jst'])) for f in state['final_confirmations']):
            continue
        if e.get('evaluation_mode') != state['mode']:
            blocks.append('SOURCE_MODE_MISMATCH'); continue
        eligible, missing = membership(c, e, cutoff)
        if missing:
            blocks.append('MEMBERSHIP_UNRESOLVED')
        if not eligible and not missing:
            continue
        r = copy.deepcopy(evidence.get(eid) or {**e, 'challenger_id':c['challenger_id'],
            'model_spec_hash':c['model_spec_hash'], 'mode':state['mode'],
            'gross_R':None,'net_R':None,'exit_result':None,'reference_resolved_at_jst':None,
            'execution_resolved_at_jst':None})
        if r.get('model_spec_hash') != c['model_spec_hash'] or r.get('mode') != state['mode']:
            blocks.append('EVIDENCE_IDENTITY_MISMATCH')
        if eid in evidence and any(r.get(k) != v for k,v in e.items()):
            blocks.append('EVIDENCE_SOURCE_MISMATCH')
        if missing or not r.get('reference_resolved_at_jst') or dt(r['reference_resolved_at_jst']) > cutoff:
            for k in list(r):
                if k.startswith(('exit_', 'time_exit_', 'ambiguous_')) or k in ('gross_R','net_R','spread_R','fee_R','slippage_R'):
                    r[k] = None
            r['exit_result']=None
        elif not r.get('execution_resolved_at_jst') or dt(r['execution_resolved_at_jst']) > cutoff:
            r['net_R']=None
        rows.append(r)
    # Evidence or membership without an authoritative source body is unresolved.
    for r in state['evidence']+state['memberships']:
        if r['challenger_id'] == c['challenger_id'] and start <= dt(r['entry_time_jst']) < cutoff and r['event_id'] not in history:
            blocks.append('SOURCE_BODY_MISSING')
    return rows, sorted(set(blocks))


def certification(state, c, start, cutoff, purpose):
    import math
    from .storage import research_blocks
    from .forward import settings_valid
    rows, blocks = population(state, c, start, cutoff, purpose)
    blocks += research_blocks(state)
    if state['mode'] != 'FORWARD':
        blocks.append('BACKFILL_NOT_FORWARD')
    if any(not r.get('exit_result') for r in rows):
        blocks.append('PENDING_REFERENCE_TRADES')
    if purpose != 'gate' and not settings_valid(state['execution_settings']):
        blocks.append('EXECUTION_COST_CONFIG_BLOCKER')
    # Gate has a reported Net-evaluable subset, not the Practice/Final 100%
    # qualification threshold. Never invent a new coverage threshold for Gate.
    if any(r.get('gross_R') is not None and ((purpose != 'gate' and r.get('net_R') is None) or
           (r.get('net_R') is not None and any(r.get(k) is None for k in ('spread_R','fee_R','slippage_R')))) for r in rows):
        blocks.append('NET_INCOMPLETE')
    for r in rows:
        if r.get('net_R') is not None:
            if not settings_valid(r.get('cost_settings', {})) or r.get('entry_execution', {}).get('expected_fill_price') is None or r.get('exit_expected_fill_price') is None or not r.get('exit_quote_timestamp'):
                blocks.append('NET_PROVENANCE_INCOMPLETE')
            elif dt(r['exit_quote_timestamp']) > dt(cutoff):
                blocks.append('NET_QUOTE_AFTER_CUTOFF')
        if r.get('net_R') is not None and all(r.get(k) is not None for k in ('gross_R','spread_R','fee_R','slippage_R')):
            if not math.isclose(r['net_R'], r['gross_R']-sum(r[k] for k in ('spread_R','fee_R','slippage_R')), abs_tol=1e-10):
                blocks.append('NET_COMPONENT_MISMATCH')
    return rows, sorted(set(blocks))
