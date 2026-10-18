import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_adaptive import T, state, challenger, event, evidence, CONFIG
from model511_adaptive.config import iso, digest, base_model
from model511_adaptive import forward, registry, workflow_save
from model511_adaptive.shadow import evaluate
from model511_adaptive.runner import run, redact_holdouts
from model511_adaptive.source_reader import exit_analysis
from model511.shadow_engine import evaluate as beta_evaluate


def bar(start, minutes=5, hi=100.1, lo=99.9):
    return {'start': iso(start), 'end': iso(start+timedelta(minutes=minutes)),
            'high': hi, 'low': lo, 'open': 100., 'close': 100.}


def observation(at):
    return {'timestamp': iso(at), 'source': 'RECORDED', 'price': 100., 'bid': 99.99, 'ask': 100.01}


class ExitBoundaries(unittest.TestCase):
    def test_sixty_frozen_exit_variants_both_sides(self):
        for side in ('LONG', 'SHORT'):
            for sl in (.25, .35, .5, .75, 1.):
                for rr in (1., 1.5, 2.):
                    for hours in (3, 6, 12, 24):
                        with self.subTest(side=side, sl=sl, rr=rr, hours=hours):
                            e = event(T, side=side)
                            spec = {'sl_pct': sl, 'rr': rr, 'max_hours': hours}
                            b = bar(T, hi=104. if side == 'LONG' else 100., lo=100. if side == 'LONG' else 96.)
                            r = evaluate(e, [b], [], T+timedelta(minutes=5), exit_spec=spec)
                            self.assertEqual(r['exit_result'], 'TP')
                            self.assertAlmostEqual(r['gross_R'], rr)
                            self.assertEqual(r['deadline_jst'], iso(T+timedelta(hours=hours)))

    def test_same_bar_tp_sl_unknown_all_horizons(self):
        for hours in (3, 6, 12, 24):
            r = evaluate(event(T), [bar(T, hi=101., lo=99.)], [], T+timedelta(minutes=5),
                         exit_spec={'sl_pct': .5, 'rr': 1., 'max_hours': hours})
            self.assertEqual(r['exit_result'], 'UNKNOWN'); self.assertIsNone(r['gross_R'])

    def test_time_waits_frozen_deadline_and_real_quote(self):
        for hours in (3, 6, 12, 24):
            spec = {'sl_pct': .5, 'rr': 1., 'max_hours': hours}
            bars = [bar(T+timedelta(minutes=5*i)) for i in range(hours*12)]
            deadline = T+timedelta(hours=hours)
            self.assertIsNone(evaluate(event(T), bars, [], deadline, exit_spec=spec))
            self.assertIsNone(evaluate(event(T), bars, [observation(deadline-timedelta(seconds=1))], deadline, exit_spec=spec))
            r = evaluate(event(T), bars, [observation(deadline)], deadline, exit_spec=spec)
            self.assertEqual(r['exit_result'], 'TIME')

    def test_boundary_touch_unknown_and_fine_resolution(self):
        start = T+timedelta(minutes=2)
        e = event(start)
        bars = [bar(T+timedelta(minutes=i)) for i in range(5, 180, 5)]
        bars.append(bar(T+timedelta(hours=3), hi=100.6))
        at = T+timedelta(hours=3, minutes=5)
        r = evaluate(e, bars, [observation(at)], at)
        self.assertEqual(r['unknown_reason'], 'DEADLINE_BOUNDARY_INSUFFICIENT_GRANULARITY')
        fine = [bar(T+timedelta(hours=3), minutes=1, hi=100.6)]
        self.assertEqual(evaluate(e, bars, [observation(at)], at, fine)['exit_result'], 'TP')

    def test_default_matches_beta1_reference(self):
        for side in ('LONG', 'SHORT'):
            for hi, lo in ((100.6, 100.), (100., 99.4), (100.6, 99.4)):
                e = event(T, side=side); bars = [bar(T, hi=hi, lo=lo)]
                a = evaluate(e, bars, [], T+timedelta(minutes=5))
                b = beta_evaluate(e, bars, [], T+timedelta(minutes=5))
                for k in ('gross_R', 'exit_result', 'exit_timestamp', 'deadline_jst', 'exit_reference_price'):
                    self.assertEqual(a[k], b[k])

    def test_missing_bar_gap_not_filled(self):
        self.assertIsNone(evaluate(event(T), [bar(T+timedelta(minutes=5), hi=104.)], [], T+timedelta(minutes=10)))

    def test_future_candle_not_used(self):
        self.assertIsNone(evaluate(event(T), [bar(T, hi=104.)], [], T+timedelta(minutes=4)))


class ExecutionBoundaries(unittest.TestCase):
    def settle(self, sl=.5, side='LONG', settings=None, quote=True):
        s = state(); m = base_model(); m['exit']['sl_pct'] = sl
        challenger(s, m)
        s['execution_settings'] = CONFIG.copy() if settings is None else settings
        at = T+timedelta(minutes=5)
        e = event(at, side=side)
        market = {'X_USDT': {'bars': [bar(at, hi=103. if side == 'LONG' else 100., lo=100. if side == 'LONG' else 97.)],
                               'observations': [observation(at+timedelta(minutes=5))] if quote else []}}
        source = {'evaluation_mode': 'FORWARD', 'events': [e]}
        forward.update(s, source, market, at+timedelta(minutes=5))
        return s, source, market

    def test_cost_components_once_each_risk_unit(self):
        for sl in (.25, .5, 1.):
            for side in ('LONG', 'SHORT'):
                s, _, _ = self.settle(sl, side); r = s['evidence'][0]
                self.assertAlmostEqual(r['net_R'], r['gross_R']-r['spread_R']-r['fee_R']-r['slippage_R'])
                sign = 1 if side == 'LONG' else -1
                fill_result = sign*(r['exit_expected_fill_price']-r['entry_execution']['expected_fill_price'])/(100*sl/100)
                self.assertAlmostEqual(r['net_R'], fill_result-r['fee_R']-r['slippage_R'])

    def test_missing_quote_gross_only(self):
        s, _, _ = self.settle(quote=False); r = s['evidence'][0]
        self.assertAlmostEqual(r['gross_R'], 1.)
        self.assertIsNone(r['spread_R']); self.assertIsNone(r['net_R'])

    def test_missing_settings_no_net(self):
        s, _, _ = self.settle(settings={}); r = s['evidence'][0]
        self.assertEqual(r['cost_status'], 'MISSING_FEE_CONFIG'); self.assertIsNone(r['net_R'])

    def test_later_config_never_fills_past_null(self):
        s, source, market = self.settle(settings={}); before = copy.deepcopy(s['evidence'])
        s['execution_settings'] = CONFIG.copy()
        forward.update(s, source, market, T+timedelta(minutes=15))
        self.assertEqual(s['evidence'], before)

    def test_reexecution_no_trade_or_cost_duplication(self):
        s, source, market = self.settle(); before = copy.deepcopy(s)
        forward.update(s, source, market, T+timedelta(minutes=10))
        self.assertEqual(s, before)

    def test_one_known_component_does_not_become_net(self):
        s, _, _ = self.settle(settings={'fee_bps_entry': 1, 'fee_bps_exit': 1})
        r = s['evidence'][0]
        self.assertIsNotNone(r['fee_R']); self.assertIsNone(r['slippage_R']); self.assertIsNone(r['net_R'])


class SourcesAndWorkflow(unittest.TestCase):
    def test_exit_variants_not_independent(self):
        source = {'exit_tables': {'research_exit_trades': [{'trade_id': 'x', 'scan_time_jst': iso(T)}],
                   'research_exit_results': [{'trade_id': 'x', 'sl_pct': sl, 'rr': rr, 'max_hours': h,
                       'exit_bar_time_jst': iso(T+timedelta(hours=1)), 'result_r': '1'}
                       for sl in (.25, .35, .5, .75, 1.) for rr in (1., 1.5, 2.) for h in (3, 6, 12, 24)]}}
        report = exit_analysis(source, T+timedelta(days=1))
        self.assertEqual(len(report['cells']), 60)
        self.assertEqual(report['unique_source_trade_n'], 1)
        self.assertTrue(all(not r['automatic_selection'] for r in report['cells']))

    def test_scan_gate_empty_reexecution(self):
        s = state(); s['scans'] = []
        source = {'rows': [], 'beta': None, 'exit_tables': {'research_exit_trades': [], 'research_exit_results': []},
                  'issues': [{'level': 'BLOCK', 'reason': 'MISSING_SOURCE'}], 'manifest': [],
                  'snapshot_id': 's', 'source_commit': 'a', 'source_schema_version': '1'}
        at = T+timedelta(days=15)
        a = run(s, source, at); b = run(a, source, at)
        self.assertEqual(a, b); self.assertEqual(len(a['scans']), 3); self.assertEqual(len(a['gate_decisions']), 1)

    def test_workflow_save_conflict_no_push_or_reset(self):
        with patch('subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'PREPARED_RECOVERY_REQUIRED'):
                workflow_save.save(Path('.'), 'base')
            run.assert_not_called()

    def test_workflow_only_saves_adaptive_paths(self):
        from model511_adaptive.persistence import owned
        self.assertTrue(owned('511_adaptive/forward/adaptive_state.json', 'adaptive'))
        for name in ('strategy_pullback.csv', '511_data/forward/state.json',
                     '511_adaptive/backfill/adaptive_state.json', '../state.json'):
            self.assertFalse(owned(name, 'adaptive'))

    def test_workflow_is_independent_and_opt_in(self):
        root = Path(__file__).resolve().parents[2]
        text = (root/'.github/workflows/model511_adaptive.yml').read_text()
        self.assertIn("vars.ADAPTIVE_511_ENABLED == 'true'", text)
        self.assertIn('workflows: [MEXC Research]', text)
        self.assertIn('if: always()', text)
        self.assertNotIn('strategy_pullback.py', text)
        self.assertNotIn('research_exit.py', text)
