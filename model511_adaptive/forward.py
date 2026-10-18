"""Freeze membership at first available event; immutable resolved outcomes."""
import copy
from datetime import timedelta
from model511.execution import entry_execution, costs
from model511.source_reader import number
from .config import dt, iso, digest, COST_KEYS
from .registry import get
from .storage import audit, require_research_ready
from .shadow import evaluate


def matches(model, event):
    missing = []
    eligible = True
    for c in model['entry']:
        value = event.get(c['field'])
        if value is None or value == '':
            missing.append(c['field'])
            eligible = False
            continue
        target = c['value']
        if c['op'] == 'eq' and isinstance(target, (int, float)) and not isinstance(target, bool):
            value = number(value)
        if c['op'] in ('ge', 'le'):
            value = number(value)
            target = number(target)
            if value is None or target is None:
                missing.append(c['field'])
                eligible = False
                continue
        ok = (value == target if c['op'] == 'eq' else value in target if c['op'] == 'in'
              else value >= target if c['op'] == 'ge' else value <= target)
        eligible = eligible and ok
    return eligible, missing


def settings_valid(settings):
    for k, v in settings.items():
        if k not in COST_KEYS or number(v) is None or number(v) < 0:
            raise ValueError('Invalid execution setting: ' + k)
    return all(number(settings.get(k)) is not None for k in COST_KEYS)


def update(state, beta, market, at):
    # Pure transaction on a copy: failures cannot leave partial population proofs.
    from .completeness import migrate
    working = migrate(copy.deepcopy(state))
    _update(working, beta, market, at)
    def adopt(old, new):
        if isinstance(old, dict) and isinstance(new, dict):
            for key in list(old):
                if key not in new:
                    del old[key]
            for key, value in new.items():
                old[key] = adopt(old.get(key), value)
            return old
        if isinstance(old, list) and isinstance(new, list):
            common = min(len(old), len(new))
            for i in range(common):
                old[i] = adopt(old[i], new[i])
            old[common:] = new[common:]
            return old
        return new
    adopt(state, working)


def _update(state, beta, market, at):
    require_research_ready(state)
    at = dt(at)
    if state['last_update_jst'] and at < dt(state['last_update_jst']):
        raise ValueError('Backward Forward time')
    if beta is None or beta['evaluation_mode'] != state['mode']:
        raise ValueError('Missing beta source or BACKFILL/FORWARD mode mismatch')
    if beta.get('last_cutoff_jst') and dt(beta['last_cutoff_jst']) > at:
        raise ValueError('Future source snapshot')
    from .completeness import ingest
    from .semantics import membership, model_blocks
    ingest(state, beta, at)
    closed = beta.get('last_processed_bucket')
    complete_through = dt(closed)+timedelta(minutes=5) if closed else None
    if complete_through and complete_through > at:
        raise ValueError('Future source population watermark')
    state['entry_population_complete_through_jst'] = iso(complete_through) if complete_through else None
    if state.get('beta_forward_start_jst') and beta.get('forward_start_jst') != state['beta_forward_start_jst']:
        raise ValueError('Beta Forward start reset detected')
    if beta.get('forward_start_jst'):
        state['beta_forward_start_jst'] = beta['forward_start_jst']
    events = {e['event_id']: e for e in beta['events']}
    results = {r['event_id']: r for r in beta.get('results', [])}
    for m in beta.get('memberships', []):
        e = events.get(m['event_id'])
        r = results.get(m['event_id'])
        if not m.get('eligible') or not e or not r or dt(e['source_cutoff_jst']) > at or dt(r['exit_timestamp']) > at:
            continue
        bid = m['model_id']+'|'+m['event_id']
        if bid not in state['benchmarks']:
            state['benchmarks'][bid] = {**copy.deepcopy(e), **copy.deepcopy(r), 'benchmark_model_id': m['model_id'],
                                        'observed_at_jst': iso(at)}
    for symbol, data in market.items():
        observations = state['observations'].setdefault(symbol, [])
        known_quotes = {digest(q) for q in observations}
        for q in data.get('observations', []):
            if dt(q['timestamp']) <= at and digest(q) not in known_quotes:
                observations.append(copy.deepcopy(q))
                known_quotes.add(digest(q))
    known = {m['membership_id'] for m in state['memberships']}
    known_evidence = {r['evidence_id'] for r in state['evidence']}
    for e in sorted(beta['events'], key=lambda r: (r['entry_time_jst'], r['event_id'])):
        if dt(e['source_cutoff_jst']) > at or dt(e['entry_time_jst']) > at:
            continue
        if e.get('evaluation_mode') != state['mode']:
            raise ValueError('Event evaluation mode mismatch')
        eid = e['event_id']
        fingerprint = digest(e)
        previous = state['seen_events'].get(eid)
        if eid in state['seen_events']:
            if not isinstance(previous, dict) or previous.get('hash') != fingerprint:
                audit(state, 'LATE_EVENT_CHANGE_IGNORED', at, event_id=eid)
            continue
        state['seen_events'][eid] = {'hash': fingerprint, 'first_seen_jst': iso(at), 'event': copy.deepcopy(e)}
        if state['last_update_jst'] and dt(e['source_cutoff_jst']) <= dt(state['last_update_jst']):
            audit(state, 'LATE_EVENT_IGNORED', at, event_id=eid)
            continue
        for c in state['challengers']:
            if dt(e['entry_time_jst']) < dt(c['forward_start_jst']):
                continue
            eligible, missing = membership(c, e, at)
            mid = c['challenger_id']+'|'+eid
            if mid in known:
                continue
            member = {'membership_id': mid, 'challenger_id': c['challenger_id'], 'event_id': eid,
                      'entry_time_jst': e['entry_time_jst'], 'model_spec_hash': c['model_spec_hash'],
                      'eligible': eligible, 'missing_features': sorted(set(missing)), 'mode': state['mode'],
                      'decision_at_jst': iso(at), 'source_cutoff_jst': e['source_cutoff_jst']}
            state['memberships'].append(member)
            known.add(mid)
            if eligible and mid not in known_evidence:
                settings = copy.deepcopy(state['execution_settings'])
                settings_valid(settings)
                state['evidence'].append({**copy.deepcopy(e), 'evidence_id': mid,
                                          'challenger_id': c['challenger_id'], 'model_spec_hash': c['model_spec_hash'],
                                          'mode': state['mode'], 'decision_at_jst': iso(at),
                                          'entry_execution': entry_execution(e), 'cost_settings': settings,
                                          'gross_R': None, 'net_R': None, 'exit_result': None,
                                          'reference_resolved_at_jst': None, 'execution_resolved_at_jst': None})
                known_evidence.add(mid)
    for row in state['evidence']:
        if row['exit_result']:
            continue
        c = get(state, 'challengers', 'challenger_id', row['challenger_id'])
        if model_blocks(c):
            continue
        data = {**market.get(row['symbol'], {}), 'observations': state['observations'].get(row['symbol'], [])}
        result = evaluate(row, data.get('bars', []), data.get('observations', []), at,
                          data.get('fine_bars', []), c['model_spec_json']['exit'])
        if result is None:
            continue
        result['execution_decision_time'] = iso(at)
        quotes = sorted((q for q in data.get('observations', [])
                         if dt(result['exit_timestamp']) <= dt(q['timestamp']) <= at
                         and number(q.get('bid')) is not None and number(q.get('ask')) is not None
                         and 0 < number(q['bid']) <= number(q['ask'])), key=lambda q: dt(q['timestamp']))
        # Legacy cost calculation has fixed .5% risk. Convert its independent cost
        # components to this frozen risk unit, then subtract exactly once.
        cost = costs(row, result, row['entry_execution'], row['cost_settings'], quotes[0] if quotes else None)
        ratio = .5/c['model_spec_json']['exit']['sl_pct']
        for k in ('spread_R', 'fee_R', 'slippage_R'):
            if cost.get(k) is not None:
                cost[k] *= ratio
        cost['net_R'] = result['gross_R']-sum(cost[k] for k in ('spread_R', 'fee_R', 'slippage_R')) if result['gross_R'] is not None and all(cost.get(k) is not None for k in ('spread_R', 'fee_R', 'slippage_R')) else None
        row.update(result)
        row.update(cost)
        row['reference_resolved_at_jst'] = iso(at)
        row['execution_resolved_at_jst'] = iso(at)
        row['trade_id'] = row['evidence_id']
    state['last_update_jst'] = iso(at)


def cohort(state, challenger_id, cutoff, start=None, purpose='forward'):
    c = get(state, 'challengers', 'challenger_id', challenger_id)
    start = max(dt(c['forward_start_jst']), dt(start)) if start else dt(c['forward_start_jst'])
    cutoff = dt(cutoff)
    if purpose == 'final':
        from .completeness import population
        return population(state, c, start, cutoff, purpose)[0]
    out = []
    candidates = list(state['evidence'])
    for row in candidates:
        if row['challenger_id'] != challenger_id or row['model_spec_hash'] != c['model_spec_hash'] or row['mode'] != state['mode']:
            continue
        if not start <= dt(row['entry_time_jst']) < cutoff:
            continue
        if purpose != 'final' and dt(row['decision_at_jst']) > cutoff:
            continue
        if purpose != 'final' and any(dt(f['final_start_jst']) <= dt(row['entry_time_jst']) and
                (not f['final_decision_count'] or dt(row['entry_time_jst']) < dt(f['evaluation_cutoff_jst']))
                for f in state['final_confirmations']):
            continue  # holdout never becomes selection evidence
        r = copy.deepcopy(row)
        if dt(r['decision_at_jst']) > cutoff or not r['reference_resolved_at_jst'] or dt(r['reference_resolved_at_jst']) > cutoff:
            for k in list(r):
                if k.startswith(('exit_', 'ambiguous_', 'time_exit_')) or k in ('gross_R', 'net_R', 'spread_R', 'fee_R', 'slippage_R', 'unknown_reason', 'execution_decision_time'):
                    r[k] = None
            for k in ('gross_R', 'net_R', 'exit_result', 'exit_timestamp', 'exit_reference_price'):
                r[k] = None
        elif r.get('execution_resolved_at_jst') and dt(r['execution_resolved_at_jst']) > cutoff:
            r['net_R'] = None
        out.append(r)
    return out


def benchmark_cohort(state, model_id, start, cutoff):
    return [copy.deepcopy(r) for r in state['benchmarks'].values() if r['benchmark_model_id'] == model_id
            and dt(start) <= dt(r['entry_time_jst']) < dt(cutoff) and dt(r['observed_at_jst']) <= dt(cutoff)
            and not any(dt(f['final_start_jst']) <= dt(r['entry_time_jst']) and (not f['final_decision_count'] or
                        dt(r['entry_time_jst']) < dt(f['evaluation_cutoff_jst'])) for f in state['final_confirmations'])]
