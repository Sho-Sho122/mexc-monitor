"""Read-only snapshots of legacy research; no guessed assets or execution data."""
import csv
import hashlib
import io
import json
from pathlib import Path
from model511.source_reader import read_snapshot, number
from model511.unique_engine import unique_events
from model511.regime_engine import regime
from model511.config import dt as legacy_dt
from .config import dt, iso, digest

FEATURE_PREFIXES = ('btc_', 'rsi', 'ema', 'macd', 'atr', 'volume_ratio', 'distance_ema')


def enrich(events, rows):
    by_hash = {r['row_hash']: r for r in rows}
    for e in events:
        ref = by_hash.get(e.get('reference_row_hash'), {})
        for k, v in ref.items():
            if k.startswith(FEATURE_PREFIXES) and k not in e and number(v) is not None:
                e[k] = number(v)
        for field in ('oi_change_pct', 'funding_rate'):
            value = number(e.get(field))
            e['oi_direction' if field == 'oi_change_pct' else 'funding_sign'] = (
                'UNKNOWN' if value is None else 'POSITIVE' if value > 0 else 'NEGATIVE' if value < 0 else 'ZERO')


def snapshot(root, beta_dir, cutoff, commit):
    root, beta_dir = Path(root), Path(beta_dir)
    issues, manifest = [], []
    try:
        rows, files, source_issues = read_snapshot(root, cutoff)
        manifest.extend(files)
        issues.extend({'level': 'WARN' if a['reason'] == 'FUTURE_SCAN_TIME' else 'BLOCK', **a} for a in source_issues)
    except (OSError, ValueError) as exc:
        rows = []
        issues.append({'level': 'BLOCK', 'reason': 'STRATEGY_SOURCE_ERROR', 'detail': str(exc)})
    payloads = {}

    def read(path, optional=False):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            issues.append({'level': 'WARN' if optional else 'BLOCK', 'reason': 'MISSING_SOURCE', 'file': str(path)})
            return None
        payloads[path] = raw
        manifest.append({'source_file': str(path), 'source_hash': hashlib.sha256(raw).hexdigest(),
                         'source_commit': commit, 'source_cutoff_jst': iso(cutoff)})
        return raw

    for strategy in ('high_edge', 'trend_volume', 'pullback', 'oi_funding'):
        raw = read(root/f'strategy_{strategy}_state.json')
        if raw:
            try:
                if not isinstance(json.loads(raw), dict):
                    raise ValueError('state must be object')
            except (ValueError, UnicodeError):
                issues.append({'level': 'BLOCK', 'reason': 'STRATEGY_STATE_INVALID', 'strategy': strategy})
    beta = None
    raw = read(beta_dir/'511_state.json')
    if raw:
        try:
            beta = json.loads(raw)
            from model511.config import SPEC_HASH
            if beta.get('spec_hash') != SPEC_HASH or beta.get('schema_version') != 1:
                raise ValueError('beta spec/schema mismatch')
            for table in ('events', 'results', 'executions'):
                values = [r['event_id'] for r in beta[table]]
                if len(values) != len(set(values)):
                    raise ValueError('duplicate ' + table)
            for e in beta['events']:
                if e.get('spec_hash') != SPEC_HASH or number(e.get('entry_price')) is None or number(e['entry_price']) <= 0 or e.get('direction') not in ('LONG', 'SHORT'):
                    raise ValueError('invalid beta event identity/price')
                if dt(e['entry_time_jst']) > dt(e['source_cutoff_jst']):
                    raise ValueError('event after source cutoff')
            event_ids = {e['event_id'] for e in beta['events']}
            if any(r['event_id'] not in event_ids for r in beta['results']):
                raise ValueError('orphan beta result')
            if beta.get('last_cutoff_jst') and dt(beta['last_cutoff_jst']) > dt(cutoff):
                issues.append({'level': 'WARN', 'reason': 'SOURCE_NEWER_THAN_CUTOFF'})
            # State is authoritative. Projection mismatches block research, never repair upstream.
            from model511.storage import TABLES
            for table in ('events', 'results', 'executions'):
                projection = read(beta_dir/TABLES[table], optional=True)
                if projection:
                    actual = list(csv.DictReader(io.StringIO(projection.decode('utf-8-sig'))))
                    expected = beta[table]
                    def stringify(r):
                        return {k: json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (dict, list)) else '' if v is None else str(v) for k, v in r.items()}
                    columns = set(actual[0]) if actual else set()
                    projected = [{k: stringify(r).get(k, '') for k in columns} for r in expected]
                    if len(actual) != len(expected) or actual != projected:
                        issues.append({'level': 'BLOCK', 'reason': 'CSV_STATE_MISMATCH', 'table': table})
        except (ValueError, KeyError, TypeError) as exc:
            issues.append({'level': 'BLOCK', 'reason': 'BETA_STATE_INVALID', 'detail': str(exc)})
            beta = None
    if beta:
        enrich(beta['events'], rows)
    exit_tables = {}
    for name in ('research_exit_trades', 'research_exit_results'):
        merged = {}
        paths = sorted(set(root.glob(name+'*.csv')))
        if not paths:
            issues.append({'level': 'BLOCK', 'reason': 'MISSING_SOURCE', 'file': name})
        for path in paths:
            raw = read(path)
            for r in csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))):
                k = (r.get('trade_id'), r.get('sl_pct'), r.get('rr'), r.get('max_hours'))
                if k in merged and merged[k] != r:
                    issues.append({'level': 'BLOCK', 'reason': 'CONFLICTING_EXIT_ROWS', 'key': list(k)})
                merged[k] = r
        exit_tables[name] = list(merged.values())
    for path, raw in payloads.items():
        if path.read_bytes() != raw:
            raise ValueError('Source changed during snapshot: ' + str(path))
    return {'rows': rows, 'beta': beta, 'exit_tables': exit_tables, 'issues': issues,
            'manifest': manifest, 'snapshot_id': digest(manifest), 'source_commit': commit,
            'source_cutoff_jst': iso(cutoff), 'source_schema_version': 'legacy-beta1-1'}


def discovery_events(source, cutoff):
    rows = [r for r in source['rows'] if legacy_dt(r['scan_time_jst']) <= dt(cutoff)]
    events, _ = unique_events(rows)
    events.sort(key=lambda e: (dt(e['scan_bucket_jst']), e['event_id']))
    enrich(events, rows)
    history = []
    for e in events:
        from datetime import timedelta
        history = [h for h in history if dt(h['scan_bucket_jst']) >= dt(e['scan_bucket_jst'])-timedelta(minutes=30)]
        e.update(regime(e, history))
        history.append(e)
    # Outcome join requires matching reference entry, event time AND exact exit variant.
    # Strategy Exit variants remain separate diagnostics, never independent evidence.
    return events


def exit_analysis(source, cutoff):
    from collections import defaultdict
    from .statistics import summarize
    from model511.config import bucket as legacy_bucket, iso as legacy_iso
    grouped = {}
    invalid = 0
    trades = {t['trade_id']: t for t in source['exit_tables']['research_exit_trades']}
    for r in source['exit_tables']['research_exit_results']:
        try:
            if legacy_dt(r['exit_bar_time_jst']) > dt(cutoff):
                continue
            if r['trade_id'] not in trades or legacy_dt(trades[r['trade_id']]['scan_time_jst']) > dt(cutoff):
                continue
            cell = (float(r['sl_pct']), float(r['rr']), int(r['max_hours']))
            group = grouped.setdefault(cell, {})
            group[r['trade_id']] = r
        except (KeyError, TypeError, ValueError):
            invalid += 1
    out = []
    sls, rrs, hours = [.25, .35, .5, .75, 1.], [1., 1.5, 2.], [3, 6, 12, 24]
    for cell, group in sorted(grouped.items()):
        values = [number(r['result_r']) for r in group.values()]
        values = [v for v in values if v is not None]
        neighbors = [list(c) for c in grouped if sum(a != b for a, b in zip(c, cell)) == 1
                     and all(abs(axis.index(a)-axis.index(b)) <= 1 for axis, a, b in zip((sls, rrs, hours), c, cell))]
        by_strategy = defaultdict(dict)
        for r in sorted(group.values(), key=lambda r: (trades[r['trade_id']]['scan_time_jst'], r['trade_id'])):
            t = trades[r['trade_id']]
            if not all(t.get(k) for k in ('strategy', 'symbol', 'direction')):
                continue
            eid = '|'.join((legacy_iso(legacy_bucket(t['scan_time_jst'])), t['symbol'], t['direction']))
            if eid in by_strategy[t['strategy']]:
                continue
            by_strategy[t['strategy']][eid] = {'event_id': eid, 'symbol': t['symbol'], 'direction': t['direction'],
                'entry_time_jst': legacy_iso(t['scan_time_jst']), 'exit_timestamp': legacy_iso(r['exit_bar_time_jst']),
                'exit_result': r.get('event'), 'gross_R': number(r['result_r']), 'net_R': None,
                'holding_hours': number(r.get('holding_hours')), 'mfe_pct': number(r.get('mfe_pct')), 'mae_pct': number(r.get('mae_pct'))}
        diagnostics = {}
        for strategy, event_rows in by_strategy.items():
            rr = list(event_rows.values())
            diagnostics[strategy] = {'metrics': summarize(rr, False), 'direction': {
                side: summarize([r for r in rr if r['direction'] == side], False) for side in ('LONG', 'SHORT')}}
            for field in ('holding_hours', 'mfe_pct', 'mae_pct'):
                vals = [r[field] for r in rr if r[field] is not None]
                diagnostics[strategy][field] = {'n': len(vals), 'mean': sum(vals)/len(vals) if vals else None}
        out.append({'sl_pct': cell[0], 'rr': cell[1], 'max_hours': cell[2], 'unique_trade_n': len(group),
                    'gross_ev': sum(values)/len(values) if values else None, 'net_ev': None,
                    'net_reason': 'NO_EXECUTION_COST_DATA', 'neighbor_cells': neighbors,
                    'diagnostic_by_strategy': diagnostics,
                    'reference_basis': 'legacy Exit Research; never imported as Adaptive Forward',
                    'automatic_selection': False, 'plateau_status': 'HUMAN_DIAGNOSTIC_REVIEW'})
    return {'cells': out, 'unique_source_trade_n': len({r['trade_id'] for group in grouped.values() for r in group.values()}),
            'variant_rows_are_independent': False, 'invalid_rows': invalid}
