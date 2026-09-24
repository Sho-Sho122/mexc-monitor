import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from datetime import timedelta

from test_beta1 import event, signal, bar, T, iso
import test_beta1
from model511.execution import costs
from model511.runner import process, settle
from model511.storage import empty
from model511.evaluation import summarize, reports, validate_blocks
from model511 import workflow_save as ws


class IndependentCosts(unittest.TestCase):
    def setUp(self):
        fixture = test_beta1.CostTests()
        fixture.setUp()
        self.e, self.r, self.x, self.q, self.c = fixture.e, fixture.r, fixture.x, fixture.q, fixture.c

    def test_spread_survives_missing_configs(self):
        out = costs(self.e, self.r, self.x, {}, self.q)
        self.assertAlmostEqual(out['spread_R'], .4)
        self.assertIsNone(out['fee_R'])
        self.assertIsNone(out['slippage_R'])
        self.assertIsNone(out['net_R'])

    def test_fee_survives_missing_slippage(self):
        config = {k: v for k, v in self.c.items() if k.startswith('fee_')}
        out = costs(self.e, self.r, self.x, config, self.q)
        self.assertIsNotNone(out['fee_R'])
        self.assertIsNone(out['slippage_R'])
        self.assertIsNone(out['net_R'])

    def test_slippage_survives_missing_fee(self):
        config = {k: v for k, v in self.c.items() if k.startswith('slippage_')}
        out = costs(self.e, self.r, self.x, config, self.q)
        self.assertIsNotNone(out['slippage_R'])
        self.assertIsNone(out['fee_R'])
        self.assertIsNone(out['net_R'])

    def test_unknown_quote_does_not_become_zero(self):
        out = costs(self.e, self.r, self.x, self.c)
        self.assertTrue(all(out[k] is None for k in ('spread_R', 'fee_R', 'slippage_R', 'net_R')))
        self.assertEqual(self.r['gross_R'], 1)

    def test_explicit_zero_config_is_valid(self):
        out = costs(self.e, self.r, self.x, dict.fromkeys(self.c, 0), self.q)
        self.assertEqual(out['fee_R'], 0)
        self.assertEqual(out['slippage_R'], 0)
        self.assertAlmostEqual(out['net_R'], .6)

    def test_delayed_quote_keeps_reference_and_records_delay(self):
        before = copy.deepcopy(self.r)
        self.r['execution_decision_time'] = iso(T+timedelta(hours=3, minutes=2))
        q = self.q | {'timestamp': iso(T+timedelta(hours=3, minutes=1))}
        out = costs(self.e, self.r, self.x, self.c, q)
        self.assertEqual(out['exit_quote_delay_ms'], 60000)
        self.assertEqual(out['exit_data_age_ms'], 60000)
        self.assertEqual(self.r['exit_reference_price'], before['exit_reference_price'])
        self.assertIsNotNone(out['net_R'])

    def test_future_quote_rejected(self):
        q = self.q | {'timestamp': iso(T+timedelta(hours=4))}
        self.assertIsNone(costs(self.e, self.r, self.x, self.c, q)['net_R'])

    def test_pre_exit_quote_rejected(self):
        q = self.q | {'timestamp': iso(T)}
        self.assertIsNone(costs(self.e, self.r, self.x, self.c, q)['net_R'])

    def test_both_methods_each_side_no_double_spread(self):
        for side in ('LONG', 'SHORT'):
            e = self.e | {'direction': side}
            r = self.r | {'exit_reference_price': 100.5 if side == 'LONG' else 99.5}
            a = costs(e, r, self.x, self.c, self.q)
            b = costs(e, r, self.x, self.c, self.q, 'fills')
            self.assertAlmostEqual(a['net_R'], b['net_R'])
            self.assertAlmostEqual(a['net_R'], r['gross_R']-a['spread_R']-a['fee_R']-a['slippage_R'])

    def test_invalid_cost_rejected_even_with_other_missing_inputs(self):
        with self.assertRaises(ValueError):
            costs(self.e, self.r, self.x, {'fee_bps_entry': -1})

    def test_runner_tp_uses_observed_quote_and_remains_frozen(self):
        start = T.replace(minute=5, second=0)
        state = process(empty('BACKFILL', 'test'), [signal()], [], [], start, 'test')
        q = {'timestamp': iso(start+timedelta(minutes=5)), 'bid': 100.4, 'ask': 100.6,
             'price': 100.5, 'source': 'FIXTURE'}
        market = {'SOL_USDT': {'bars': [bar(start, high=100.6)], 'observations': [q]}}
        done = settle(state, market, start+timedelta(minutes=6), self.c)
        result = done['results'][0]
        self.assertEqual(result['exit_result'], 'TP')
        self.assertAlmostEqual(result['exit_reference_price'], 100.5)
        self.assertIsNotNone(result['net_R'])
        self.assertEqual(done['executions'][0]['exit_bid'], 100.4)
        again = settle(done, market, start+timedelta(minutes=7), self.c)
        self.assertEqual(done['results'], again['results'])

    def test_forward_long_short_tp_sl_execution_does_not_change_research(self):
        start = T.replace(minute=5, second=0)
        for side in ('LONG', 'SHORT'):
            for kind in ('TP', 'SL'):
                with self.subTest(side=side, kind=kind):
                    boot = process(empty('FORWARD', 'test'), [], [], [], T-timedelta(minutes=10), 'test')
                    state = process(boot, [signal(side=side)], [], [], start, 'test')
                    rising = (side == 'LONG') == (kind == 'TP')
                    reference = 100.5 if rising else 99.5
                    q = {'timestamp': iso(start+timedelta(minutes=5, seconds=1)),
                         'bid': reference-.1, 'ask': reference+.1, 'price': reference, 'source': 'FIXTURE'}
                    market = {'SOL_USDT': {'bars': [bar(start, high=100.6 if rising else 100.1,
                                                       low=99.9 if rising else 99.4)], 'observations': [q]}}
                    done = settle(state, market, start+timedelta(minutes=6), self.c)
                    outcome = done['results'][0]
                    self.assertEqual(outcome['exit_result'], kind)
                    self.assertAlmostEqual(outcome['gross_R'], 1 if kind == 'TP' else -1)
                    self.assertAlmostEqual(outcome['exit_reference_price'], reference)
                    self.assertEqual(outcome['exit_quote_delay_ms'], 1000)
                    self.assertIsNotNone(outcome['net_R'])

    def test_backfill_without_exit_quotes_preserves_gross_only(self):
        start = T.replace(minute=5, second=0)
        state = process(empty('BACKFILL', 'test'), [signal()], [], [], start, 'test')
        market = {'SOL_USDT': {'bars': [bar(start, high=100.6)], 'observations': []}}
        outcome = settle(state, market, start+timedelta(minutes=6), self.c)['results'][0]
        self.assertAlmostEqual(outcome['gross_R'], 1)
        self.assertIsNone(outcome['spread_R'])
        self.assertIsNone(outcome['net_R'])


class CoverageAndBlocks(unittest.TestCase):
    def rows(self):
        return [dict(event_id=str(i), gross_R=1., net_R=0.5, exit_result='TP',
                     exit_timestamp=iso(T), date='2026-10-01', cluster_5m_id=str(i),
                     cluster_30m_id=str(i)) for i in range(100)]

    def test_partial_coverage_cannot_support(self):
        rows = self.rows()
        rows[0]['net_R'] = None
        out = summarize(rows)
        self.assertEqual(out['resolved_gross_n'], 100)
        self.assertEqual(out['net_evaluable_n'], 99)
        self.assertEqual(out['net_coverage_ratio'], .99)
        self.assertIsNone(out['net_EV'])
        self.assertEqual(out['qualification'], 'INCONCLUSIVE')

    def test_unknown_excluded_from_coverage_denominator(self):
        rows = self.rows()+[self.rows()[0] | {'gross_R': None, 'net_R': None, 'exit_result': 'UNKNOWN'}]
        self.assertEqual(summarize(rows)['net_coverage_ratio'], 1)

    def test_empty_coverage_is_null(self):
        self.assertIsNone(summarize([])['net_coverage_ratio'])

    def test_blocks_half_open_descriptive_and_do_not_mutate(self):
        state = process(empty('BACKFILL', 'test'), [signal()], [], [], T+timedelta(minutes=3), 'test')
        state['results'] = [self.rows()[0] | {'event_id': state['events'][0]['event_id']}]
        before = copy.deepcopy(state)
        blocks = [{'name': 'includes', 'start': iso(T), 'end': iso(T+timedelta(minutes=1))},
                  {'name': 'excludes', 'start': iso(T-timedelta(minutes=1)), 'end': iso(T)}]
        result = reports(state, blocks=blocks)
        selected = [r for r in result if r['slice'] == 'WALK_FORWARD_BLOCK' and r['model_id'] == 'CTRL_PULLBACK_ANY']
        self.assertEqual([r['N'] for r in selected], [1, 0])
        self.assertTrue(all(r['qualification'] == 'DESCRIPTIVE_BLOCK_ONLY' for r in selected))
        self.assertEqual(before, state)

    def test_invalid_blocks_rejected(self):
        with self.assertRaises(ValueError):
            validate_blocks([{'name': 'bad', 'start': iso(T), 'end': iso(T)}])


class WorkflowIsolation(unittest.TestCase):
    def test_archive_error_records_failure_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('sys.argv', ['workflow_save', 'archive', '--backup', tmp]), patch.object(ws, 'archive', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    ws.main()
            row = json.loads((Path(tmp)/'save_audit.jsonl').read_text())
            self.assertEqual(row['status'], 'FAILED')
            self.assertIn('disk full', row['detail'])

    def test_archive_and_conflict_never_touch_legacy_or_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'repo'; backup = Path(tmp)/'backup'
            (root/'511_data').mkdir(parents=True); backup.mkdir()
            (root/'511_data/state.json').write_text('{}')
            (root/'strategy_pullback.csv').write_text('unchanged')
            with patch.object(ws, 'git', return_value='base'):
                ws.archive(root, backup)
            with patch.object(ws, 'git', side_effect=['', '511_data/state.json']) as fake:
                with self.assertRaisesRegex(RuntimeError, '511_REMOTE_CONFLICT'):
                    ws.save(root, backup)
            self.assertEqual([c.args[1] for c in fake.call_args_list], ['fetch', 'diff'])
            self.assertEqual((root/'strategy_pullback.csv').read_text(), 'unchanged')
            self.assertTrue((backup/'state.tar').exists())

    def test_archive_failure_has_no_git_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'repo'; backup = Path(tmp)/'backup'
            (root/'511_data').mkdir(parents=True); backup.mkdir()
            with patch.object(ws, 'git', return_value='base') as fake, patch.object(ws.tarfile, 'open', side_effect=OSError('full')):
                with self.assertRaises(OSError):
                    ws.archive(root, backup)
            self.assertEqual([c.args[1:] for c in fake.call_args_list], [('rev-parse', 'HEAD')])

    def test_save_retries_and_stages_only_511(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'repo'; backup = Path(tmp)/'backup'
            (root/'511_data').mkdir(parents=True); backup.mkdir()
            (root/'511_data/state.json').write_text('{"retained":true}')
            with patch.object(ws, 'git', return_value='base'):
                ws.archive(root, backup)
            calls = []; pushes = []
            def fake(root, *args):
                calls.append(args)
                if args[0] == 'diff':
                    return '511_data/state.json' if '--cached' in args else ''
                if args[0] == 'push':
                    pushes.append(args)
                    if len(pushes) == 1:
                        raise subprocess.CalledProcessError(1, 'mock push')
                return ''
            with patch.object(ws, 'git', side_effect=fake):
                ws.save(root, backup)
            self.assertEqual(len(pushes), 2)
            self.assertTrue(all(c == ('add', '--', '511_data') for c in calls if c[0] == 'add'))
            self.assertEqual((root/'511_data/state.json').read_text(), '{"retained":true}')

    def test_workflow_legacy_save_is_independent(self):
        root = Path(__file__).resolve().parents[2]
        text = (root/'.github/workflows/research.yml').read_text()
        legacy = text.split('      - name: Save research state robustly')[1].split('      - name: Save 511 independently')[0]
        self.assertNotIn('511_data', legacy)
        self.assertNotIn('archive511.outcome', legacy)
        self.assertNotIn('model511.outcome', legacy)
        self.assertIn("steps.research_save.outcome == 'success'", text)
        self.assertIn("steps.archive511.outcome == 'success'", text)
        scripts = ['mexc_scanner_research.py', 'strategy_high_edge.py', 'strategy_trend_volume.py',
                   'strategy_pullback.py', 'strategy_oi_funding.py', 'python -m model511 --mode', 'research_exit.py']
        positions = [text.index(s) for s in scripts]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(text.index('Preserve 511 recovery evidence'), text.index('Save research state robustly'))
        for start, end in [('Archive 511 independently', 'Preserve 511 recovery evidence'),
                           ('Preserve 511 recovery evidence', 'Save research state robustly'),
                           ('Save 511 independently', 'Preserve 511 save audit')]:
            section = text.split('      - name: '+start)[1].split('      - name: '+end)[0]
            self.assertIn('continue-on-error: true', section)
        for step in ('model511', 'archive511', 'recovery511', 'save511'):
            self.assertIn("steps."+step+".outcome == 'failure'", text)


if __name__ == '__main__':
    unittest.main()
