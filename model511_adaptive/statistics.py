"""Descriptive statistics; resample whole time clusters across symbols/directions."""
import random
from collections import defaultdict
from statistics import mean
from .config import BOOTSTRAP, bucket, dt, iso


def key(row, unit='30m_time'):
    t = dt(row['entry_time_jst'])
    return t.date().isoformat() if unit == 'day' else iso(bucket(t, 30 if unit == '30m_time' else 5))


def cluster_bootstrap(rows, unit='30m_time', samples=BOOTSTRAP['samples'], seed=BOOTSTRAP['seed']):
    if not rows or any(r.get('net_R') is None for r in rows):
        return {'ci': None, 'p_positive': None, 'reason': 'NET_INCOMPLETE'}
    groups = defaultdict(list)
    for r in rows:
        groups[key(r, unit)].append(r['net_R'])
    if len(groups) < 2:
        return {'ci': None, 'p_positive': None, 'reason': 'INSUFFICIENT_CLUSTERS'}
    values = {r['net_R'] for r in rows}
    if len(values) == 1:
        v = next(iter(values))
        return {'ci': [v, v], 'p_positive': 1/(samples+1) if v > 0 else 1.,
                'reason': None, 'unit': unit, 'samples': samples, 'seed': seed}
    groups = [(sum(v), len(v)) for _, v in sorted(groups.items())]
    observed = mean(r['net_R'] for r in rows)
    rng = random.Random(seed)
    estimates = []
    for _ in range(samples):
        selected = [groups[rng.randrange(len(groups))] for _ in groups]
        estimates.append(sum(g[0] for g in selected)/sum(g[1] for g in selected))
    estimates.sort()
    # Null-centered cluster bootstrap: P(mean* - mean >= observed).
    p = (1 + sum(v-observed >= observed for v in estimates))/(samples+1)
    return {'ci': [estimates[int(samples*.025)], estimates[min(samples-1, int(samples*.975))]],
            'p_positive': p, 'reason': None, 'unit': unit, 'samples': samples, 'seed': seed}


def holm(pvalues, total_candidates):
    if total_candidates is None or total_candidates < len(pvalues) or not pvalues or any(v is None for v in pvalues.values()):
        return {'status': 'MULTIPLE_TESTING_UNRESOLVED', 'adjusted': {}}
    adjusted, floor = {}, 0.
    for i, (candidate, p) in enumerate(sorted(pvalues.items(), key=lambda x: (x[1], x[0]))):
        floor = max(floor, min(1., (total_candidates-i)*p))
        adjusted[candidate] = floor
    return {'status': 'HOLM_COMPUTED', 'adjusted': adjusted, 'candidate_count': total_candidates,
            'unobserved_candidates': total_candidates-len(pvalues), 'selection_rule': 'DIAGNOSTIC_NOT_RANKING'}


def streak(values):
    current = longest = 0
    for v in values:
        current = current+1 if v < 0 else 0
        longest = max(longest, current)
    return longest


def summarize(rows, bootstrap=True):
    ids = [r['event_id'] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate events are not independent samples')
    resolved = [r for r in rows if r.get('gross_R') is not None]
    net = [r for r in resolved if r.get('net_R') is not None]
    n, net_n = len(resolved), len(net)
    complete = n > 0 and n == net_n
    equity = peak = dd = 0.
    ordered = sorted(resolved, key=lambda r: (r['exit_timestamp'], r['event_id']))
    for r in ordered:
        equity += r['gross_R']
        peak = max(peak, equity)
        dd = max(dd, peak-equity)
    out = {'unique_event_n': len(rows), 'resolved_unique_events': n, 'resolved_gross_n': n,
           'net_evaluable_n': net_n, 'net_coverage_ratio': net_n/n if n else None,
           'formal_net_complete': complete, 'gross_ev': mean(r['gross_R'] for r in resolved) if n else None,
           'net_ev': mean(r['net_R'] for r in net) if complete else None,
           'net_evaluable_ev_diagnostic': mean(r['net_R'] for r in net) if net else None,
           'gross_R': sum(r['gross_R'] for r in resolved),
           'net_R': sum(r['net_R'] for r in net) if complete else None,
           'time_cluster_30m_n': len({key(r) for r in resolved}),
           'time_cluster_5m_n': len({key(r, '5m_time') for r in resolved}),
           'direction_cluster_30m_n': len({(key(r), r['direction']) for r in resolved}),
           'active_days': len({key(r, 'day') for r in resolved}),
           'UNKNOWN': sum(r.get('exit_result') == 'UNKNOWN' for r in rows),
           'pending_reference_n': sum(not r.get('exit_result') for r in rows),
           'WIN': sum(r.get('exit_result') == 'TP' for r in rows),
           'LOSS': sum(r.get('exit_result') == 'SL' for r in rows),
           'TIME': sum(r.get('exit_result') == 'TIME' for r in rows),
           'win_rate': sum(r['gross_R'] > 0 for r in resolved)/n if n else None,
           'max_drawdown_R': dd, 'drawdown_basis': 'GROSS',
           'max_event_loss_streak': streak([r['gross_R'] for r in ordered])}
    out['UNKNOWN_ratio'] = out['UNKNOWN']/len(rows) if rows else None
    net_equity = net_peak = net_dd = 0.
    if complete:
        for r in ordered:
            net_equity += r['net_R']
            net_peak = max(net_peak, net_equity)
            net_dd = max(net_dd, net_peak-net_equity)
    out['net_max_drawdown_R'] = net_dd if complete else None
    out['net_max_event_loss_streak'] = streak([r['net_R'] for r in ordered]) if complete else None
    for unit, prefix in [('5m_time', '5m'), ('30m_time', '30m')]:
        groups = defaultdict(list)
        for r in resolved:
            groups[key(r, unit)].append(r['gross_R'])
        out[prefix+'_cluster_ev'] = mean(mean(v) for v in groups.values()) if groups else None
        out['max_negative_'+prefix+'_cluster_streak'] = streak([sum(groups[k]) for k in sorted(groups)])
        ngroups = defaultdict(list)
        if complete:
            for r in resolved:
                ngroups[key(r, unit)].append(r['net_R'])
        out['net_max_negative_'+prefix+'_cluster_streak'] = streak([sum(ngroups[k]) for k in sorted(ngroups)]) if complete else None
    if bootstrap:
        out['bootstrap_30m'] = cluster_bootstrap(resolved)
        out['bootstrap_day'] = cluster_bootstrap(resolved, 'day')
    return out


def robustness(rows, anchor):
    resolved = [r for r in rows if r.get('gross_R') is not None]
    symbols = defaultdict(list)
    for r in resolved:
        symbols[r['symbol']].append(r)
    # Gross and Net are separated; unknown Net never falls back to Gross.
    result = {'basis': 'diagnostic only; no inferred BLOCK thresholds'}
    for field in ('gross_R', 'net_R'):
        complete = bool(resolved) and all(r.get(field) is not None for r in resolved)
        if not complete:
            result[field] = {'reason': 'INCOMPLETE'}
            continue
        profit = {s: sum(r[field] for r in rr) for s, rr in symbols.items()}
        ordered = sorted(profit, key=lambda s: (-profit[s], s))
        positive_total = sum(max(0, x) for x in profit.values())
        rest = [r[field] for r in resolved if r['symbol'] not in ordered[:3]]
        result[field] = {'top1_symbol_profit_share': max(0, profit[ordered[0]])/positive_total if positive_total else None,
                         'top3_symbol_profit_share': sum(max(0, profit[s]) for s in ordered[:3])/positive_total if positive_total else None,
                         'share_denominator': 'sum positive symbol profits',
                         'remove_top3_symbols_ev': mean(rest) if rest else None,
                         'symbol_positive_count': sum(v > 0 for v in profit.values()),
                         'symbol_negative_count': sum(v < 0 for v in profit.values()),
                         'leave_one_symbol_out_ev': {s: mean(r[field] for r in resolved if r['symbol'] != s)
                                                    if len(symbols) > 1 else None for s in symbols}}
    for field in ('direction', 'crowding_state', 'persistence_category', 'asset_class',
                  'btc_direction', 'btc_volatility', 'atr_regime', 'volume_regime', 'oi_direction', 'funding_sign'):
        groups = defaultdict(list)
        for r in rows:
            groups[str(r.get(field, 'UNKNOWN'))].append(r)
        result[field] = {k: summarize(v, False) for k, v in sorted(groups.items())}
    blocks = defaultdict(list)
    for r in rows:
        blocks[int((dt(r['entry_time_jst'])-dt(anchor)).total_seconds()//(7*86400))].append(r)
    result['7d_blocks'] = {str(k): summarize(v, False) for k, v in sorted(blocks.items())}
    result['leave_one_7d_block_out'] = {str(k): summarize([r for j, rr in blocks.items() if j != k for r in rr], False) for k in blocks}
    from model511.source_reader import number
    features = sorted({k for r in rows for k in r if k.startswith(('btc_', 'rsi', 'atr', 'volume_ratio', 'oi', 'funding'))})
    result['numeric_feature_observations'] = {}
    for field in features:
        values = [number(r.get(field)) for r in rows]
        values = [v for v in values if v is not None]
        result['numeric_feature_observations'][field] = {'n': len(values), 'missing_n': len(rows)-len(values),
            'mean': mean(values) if values else None, 'min': min(values) if values else None, 'max': max(values) if values else None}
    return result
