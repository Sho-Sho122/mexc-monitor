"""CLI: no network orders and no arbitrary as-of clock for formal Forward."""
import argparse
import copy
import json
import subprocess
from pathlib import Path
from datetime import timedelta
from .config import dt, iso, now, canonical, digest
from . import storage, registry, forward, gate, final_confirmation
from .source_reader import snapshot
from .runner import run
from .reporting import report


def parser():
    p = argparse.ArgumentParser(description='511 Adaptive v1.1 FINAL — shadow only')
    p.add_argument('command', choices=('init', 'update', 'scan', 'gate', 'tick', 'report', 'validate-state',
                   'export', 'record-trial', 'register', 'freeze', 'reject', 'approve', 'start-final', 'final', 'set-cost-config', 'register-feature'))
    p.add_argument('--source-dir', type=Path, default=Path('.'))
    p.add_argument('--state-dir', type=Path, default=Path('511_adaptive'))
    p.add_argument('--beta-dir', type=Path, default=Path('511_data/forward'))
    p.add_argument('--mode', choices=('FORWARD', 'BACKFILL'), default='FORWARD')
    p.add_argument('--as-of')
    p.add_argument('--request', type=Path)
    p.add_argument('--market-file', type=Path)
    p.add_argument('--fetch-market', action='store_true')
    p.add_argument('--id')
    p.add_argument('--gate-id')
    p.add_argument('--daily-time-jst')
    p.add_argument('--commit', help='Required provenance when source is an exported snapshot, otherwise read Git HEAD')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.mode == 'FORWARD' and args.as_of:
        p.error('--as-of is forbidden for formal Forward')
    if args.mode == 'BACKFILL' and (not args.as_of or args.fetch_market):
        p.error('BACKFILL requires --as-of; live retrieval is forbidden')
    at = dt(args.as_of) if args.as_of else now()
    directory = args.state_dir / args.mode.lower()
    if directory.resolve() == args.beta_dir.resolve() or args.state_dir.resolve() == args.source_dir.resolve():
        p.error('Adaptive and legacy state paths must be separate')
    request = json.loads(args.request.read_text('utf-8')) if args.request else {}
    if args.command in ('report', 'validate-state'):
        state = storage.read(directory)
        if state['mode'] != args.mode:
            raise ValueError('State mode mismatch')
        print(canonical({'valid': True} if args.command == 'validate-state' else report(state, at)))
        return
    commit = args.commit or subprocess.check_output(['git', '-c', f'safe.directory={args.source_dir.resolve().as_posix()}',
                    '-C', str(args.source_dir.resolve()), 'rev-parse', 'HEAD'], text=True).strip()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with storage.lock(directory):
            if args.command == 'init':
                if (directory/'adaptive_state.json').exists():
                    raise ValueError('State exists; never reset the anchor')
                state = storage.empty(at, args.mode, commit)
            else:
                state = storage.read(directory)
                if state['mode'] != args.mode:
                    raise ValueError('State mode mismatch')
            before = copy.deepcopy(state)
            if args.command == 'register-feature':
                prior = state['feature_catalog'].get(request.get('name'))
                if prior and prior.get('definition') == request.get('definition') and prior.get('version', '1') == str(request.get('version', '1')):
                    from .semantics import register_feature
                    register_feature(state, request, at)
                    print(canonical({'command': args.command, 'status': 'IDEMPOTENT_NO_OP'}))
                    return
            if args.command in ('register', 'freeze', 'approve', 'start-final', 'register-feature'):
                source = snapshot(args.source_dir, args.beta_dir, at, commit)
                storage.record_integrity(state, source, at)
                if source.get('beta') is not None and not any(i['level'] == 'BLOCK' for i in source['issues']):
                    from .completeness import ingest
                    if source['beta']['evaluation_mode'] != state['mode']:
                        raise ValueError('Source mode mismatch')
                    ingest(state, source['beta'], at)
                # Persist a detected BLOCK even when the following transition refuses.
                storage.write(directory, state)
                storage.require_research_ready(state)
            if args.command in ('update', 'tick', 'scan', 'gate', 'final'):
                source = snapshot(args.source_dir, args.beta_dir, at, commit)
                storage.record_integrity(state, source, at)
                if args.command in ('update', 'tick'):
                    if any(i['level'] == 'BLOCK' for i in source['issues']):
                        storage.audit(state, 'UPDATE_BLOCKED', at, issues=source['issues'])
                    else:
                        storage.require_research_ready(state)
                        market = json.loads(args.market_file.read_text('utf-8')) if args.market_file else {}
                        forward.update(state, source['beta'], market, at)
                        # Persist membership before any network fetch/restart.
                        storage.write(directory, state)
                        if args.fetch_market:
                            from model511.market_data import PublicMarketData
                            client = PublicMarketData()
                            pending = {}
                            for r in state['evidence']:
                                if not r['exit_result']:
                                    pending.setdefault(r['symbol'], []).append(r)
                            for symbol, rows in pending.items():
                                try:
                                    market[symbol] = client.fetch(symbol, min(dt(r['entry_time_jst']) for r in rows), now(),
                                        max(dt(r['entry_time_jst'])+timedelta(hours=registry.get(state, 'challengers', 'challenger_id', r['challenger_id'])['model_spec_json']['exit']['max_hours'], minutes=5) for r in rows))
                                except Exception as exc:
                                    storage.audit(state, 'MARKET_DATA_ERROR', now(), symbol=symbol, error=str(exc))
                            at = now()
                            forward.update(state, source['beta'], market, at)
                if args.command != 'update':
                    state = run(state, source, at, scans=args.command in ('tick', 'scan'),
                                gates=args.command in ('tick', 'gate'), finals=args.command in ('tick', 'final'))
            elif args.command == 'record-trial':
                registry.trial(state, request, at)
            elif args.command == 'register':
                registry.register(state, request, at)
            elif args.command == 'freeze':
                registry.freeze(state, args.id, at, commit)
            elif args.command == 'reject':
                registry.reject(state, args.id, request['reason'], at)
            elif args.command == 'approve':
                gate.promote(state, args.gate_id, args.id, request, at)
            elif args.command == 'start-final':
                final_confirmation.start(state, args.id, args.daily_time_jst, at)
            elif args.command == 'set-cost-config':
                if not request.get('source') or not request.get('approved_by'):
                    raise ValueError('Official setting source and approver required')
                forward.settings_valid(request['settings'])
                state['execution_settings'] = request['settings']
                storage.audit(state, 'EXECUTION_SETTINGS_PROSPECTIVE', at, **request)
            elif args.command == 'register-feature':
                from .semantics import register_feature
                register_feature(state, request, at)
            if state != before:
                state['revision'] += 1
            storage.write(directory, state)
            print(canonical({'command': args.command, 'mode': args.mode, 'revision': state['revision'],
                             'scans': len(state['scans']), 'gates': len(state['gate_decisions']),
                             'events': len(state['evidence']), 'output': str(directory)}))
    except Exception as exc:
        # Independent evidence; never run git reset/restore or change legacy state.
        failure = {'at_jst': iso(at), 'command': args.command, 'error': type(exc).__name__, 'message': str(exc)}
        storage.atomic(directory/'last_failure.json', canonical(failure).encode())
        raise


if __name__ == '__main__':
    main()
