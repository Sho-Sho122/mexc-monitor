"""Precommit findings: assertions added before the production fixes."""
import copy
import json
import random
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from test_adaptive import T, state, challenger, evidence, event, prepared_final
from model511_adaptive import forward, registry, gate, final_confirmation as final
from model511_adaptive.config import iso
from model511_adaptive.source_reader import discovery_events
from model511_adaptive.storage import read, write, validate
from model511_adaptive.reporting import report
from model511_adaptive.__main__ import main


def block(s, at):
    s['scans'].append({'scan_id': 'BLOCK_LATEST', 'scheduled_at_jst': iso(at),
        'source_cutoff_jst': iso(at), 'data_quality_status': 'BLOCK_RESEARCH',
        'report': {'data_quality': [{'level': 'BLOCK', 'reason': 'INVALID_SCAN_TIME'},
                                    {'level': 'BLOCK', 'reason': 'MISSING_SOURCE'}]}})


class FinalPopulationRegression(unittest.TestCase):
    def test_incomplete_source_population_cannot_pass_before_entry_arrives(self):
        s, c, f, at = prepared_final(); evidence(s, c, 14, 12, at)
        cutoff = at+timedelta(days=14)
        s['entry_population_complete_through_jst'] = iso(cutoff-timedelta(minutes=5))
        final.evaluate(s, f['final_id'], cutoff)
        self.assertEqual(f['final_decision_count'], 0)

    def test_unknown_population_watermark_cannot_pass(self):
        s, c, f, at = prepared_final(); evidence(s, c, 14, 12, at)
        s['entry_population_complete_through_jst'] = None
        final.evaluate(s, f['final_id'], at+timedelta(days=14))
        self.assertEqual(f['final_decision_count'], 0)

    def setup_late(self):
        s, c, f, at = prepared_final()
        evidence(s, c, 14, 12, at)
        cutoff = at+timedelta(days=14)
        e = event(cutoff-timedelta(minutes=1), 'late-entry')
        forward.update(s, {'evaluation_mode': 'FORWARD', 'events': list(s['source_history'].values())+[e]}, {}, cutoff+timedelta(minutes=5))
        return s, c, f, cutoff, e

    def test_late_membership_pending_cannot_pass(self):
        s, c, f, cutoff, e = self.setup_late()
        final.evaluate(s, f['final_id'], cutoff+timedelta(minutes=5))
        self.assertEqual(f['status'], 'FINAL_INSUFFICIENT_DATA')
        self.assertIn(e['event_id'], f['evaluations'][-1]['pending_reference_event_ids'])

    def test_late_result_not_used_at_earlier_cutoff(self):
        s, c, f, cutoff, e = self.setup_late()
        r = next(r for r in s['evidence'] if r['event_id'] == e['event_id'])
        r.update(exit_result='TP', gross_R=1., net_R=.9, exit_timestamp=iso(cutoff+timedelta(minutes=2)),
                 reference_resolved_at_jst=iso(cutoff+timedelta(minutes=5)),
                 execution_resolved_at_jst=iso(cutoff+timedelta(minutes=5)))
        rows = forward.cohort(s, c['challenger_id'], cutoff, f['final_start_jst'], purpose='final')
        matching = [r for r in rows if r['event_id'] == e['event_id']]
        self.assertEqual(len(matching), 1)
        self.assertIsNone(matching[0]['gross_R'])
        self.assertIsNone(matching[0]['net_R'])

    def test_missing_evidence_materialization_still_pending(self):
        s, c, f, cutoff, e = self.setup_late()
        s['evidence'] = [r for r in s['evidence'] if r['event_id'] != e['event_id']]
        final.evaluate(s, f['final_id'], cutoff+timedelta(minutes=5))
        self.assertEqual(f['final_decision_count'], 0)
        self.assertIn(e['event_id'], f['evaluations'][-1]['pending_reference_event_ids'])

    def test_restart_and_repeat_preserve_pending_population(self):
        s, c, f, cutoff, e = self.setup_late()
        with tempfile.TemporaryDirectory() as d:
            write(d, s); s = read(d)
        f = final.evaluate(s, f['final_id'], cutoff+timedelta(minutes=5))
        before = copy.deepcopy(s)
        final.evaluate(s, f['final_id'], cutoff+timedelta(hours=1))
        self.assertEqual(s, before)
        self.assertEqual(f['final_decision_count'], 0)

    def test_source_entry_survives_missing_membership_and_evidence(self):
        s, c, f, cutoff, e = self.setup_late()
        s['evidence'] = [r for r in s['evidence'] if r['event_id'] != e['event_id']]
        s['memberships'] = []
        rows = forward.cohort(s, c['challenger_id'], cutoff, f['final_start_jst'], purpose='final')
        self.assertIn(e['event_id'], [r['event_id'] for r in rows])
        # Reconstruction is a pending assessment, never a new persisted membership.
        self.assertEqual(s['memberships'], [])

    def test_wait_then_resolve_at_next_daily_cutoff_once(self):
        s, c, f, cutoff, e = self.setup_late()
        original_count = len(s['evidence'])
        s['entry_population_complete_through_jst'] = iso(cutoff)
        final.evaluate(s, f['final_id'], cutoff+timedelta(minutes=5))
        self.assertEqual(f['final_decision_count'], 0)
        r = next(r for r in s['evidence'] if r['event_id'] == e['event_id'])
        r.update(exit_result='TP', gross_R=1., net_R=.9, exit_timestamp=iso(cutoff+timedelta(minutes=10)),
                 spread_R=.04, fee_R=.03, slippage_R=.03,
                 exit_expected_fill_price=100.5, exit_quote_timestamp=iso(cutoff+timedelta(minutes=10)),
                 reference_resolved_at_jst=iso(cutoff+timedelta(minutes=10)),
                 execution_resolved_at_jst=iso(cutoff+timedelta(minutes=10)))
        final.evaluate(s, f['final_id'], cutoff+timedelta(hours=1))
        self.assertEqual(f['final_decision_count'], 0)
        s['entry_population_complete_through_jst'] = iso(cutoff+timedelta(days=1))
        final.evaluate(s, f['final_id'], cutoff+timedelta(days=1))
        self.assertEqual(f['final_decision'], 'PASS')
        self.assertIn(e['event_id'], f['evaluations'][-1]['evaluated_event_ids'])
        self.assertEqual(len(s['evidence']), original_count)
        self.assertEqual(len(f['evaluations'][-1]['evaluated_event_ids']), 169)

    def test_nonfinal_cutoff_and_discovery_boundary_unchanged(self):
        s, c, f, cutoff, e = self.setup_late()
        s['final_confirmations'] = []
        rows = forward.cohort(s, c['challenger_id'], cutoff)
        self.assertNotIn(e['event_id'], [r['event_id'] for r in rows])
        early = event(T-timedelta(days=1), 'discovery')
        s['seen_events']['discovery'] = {'event': early, 'first_seen_jst': iso(cutoff)}
        rows = forward.cohort(s, c['challenger_id'], cutoff, purpose='final')
        self.assertNotIn('discovery', [r['event_id'] for r in rows])


class ResearchGuardRegression(unittest.TestCase):
    def test_cli_refreshes_integrity_for_freeze_approve_and_start_final(self):
        for command in ('freeze', 'approve', 'start-final'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as d:
                s = state(); c = challenger(s)
                at = T+timedelta(days=32)
                args = [command, '--state-dir', d, '--source-dir', d, '--commit', 'commit-a']
                # Keep source-dir distinct from state-dir as required by CLI.
                args[args.index('--source-dir')+1] = str(Path(d)/'sources')
                if command == 'freeze':
                    s['challengers'] = []; s['hypotheses'][0]['status'] = 'REGISTERED'
                    args += ['--id', c['hypothesis_id']]
                elif command == 'approve':
                    evidence(s, c, 11); g = gate.evaluate_gate(s, at, at)
                    request = Path(d)/'approval.json'
                    request.write_text(json.dumps({'actor': 'human', 'reason': 'test', 'confirmed_human': True}))
                    args += ['--id', c['challenger_id'], '--gate-id', g['gate_id'], '--request', str(request)]
                else:
                    s, c, f, unused = prepared_final(); s['final_confirmations'] = []
                    args += ['--id', c['challenger_id'], '--daily-time-jst', '00:00:00']
                directory = Path(d)/'forward'; write(directory, s)
                source = {'snapshot_id': 'bad-source', 'issues': [{'level': 'BLOCK', 'reason': 'MISSING_SOURCE'}]}
                with patch('model511_adaptive.__main__.snapshot', return_value=source), patch('model511_adaptive.__main__.now', return_value=at):
                    with self.assertRaisesRegex(ValueError, 'BLOCK_RESEARCH'):
                        main(args)
                loaded = read(directory)
                self.assertEqual(loaded['research_integrity']['data_quality_status'], 'BLOCK_RESEARCH')
                for table in ('challengers', 'betas', 'final_confirmations'):
                    self.assertEqual(loaded[table], s[table])

    def test_direct_update_cannot_bypass_block(self):
        s = state(); challenger(s); block(s, T+timedelta(hours=1))
        before = copy.deepcopy(s)
        with self.assertRaisesRegex(ValueError, 'BLOCK_RESEARCH'):
            forward.update(s, {'evaluation_mode': 'FORWARD', 'events': []}, {}, T+timedelta(days=1))
        self.assertEqual(s, before)

    def test_gate_diagnostic_never_emits_eligible_when_blocked(self):
        s = state(); c = challenger(s); evidence(s, c, 11)
        at = T+timedelta(days=12); block(s, at)
        g = gate.evaluate_gate(s, at, at)
        self.assertEqual(g['candidates'][0]['status'], 'INVALID_DATA')
        self.assertEqual(g['decision'], 'NO_CHANGE')

    def test_freeze_direct_function_blocked(self):
        s = state(); c = challenger(s)
        s['challengers'] = []; s['hypotheses'][0]['status'] = 'REGISTERED'
        block(s, T+timedelta(hours=1)); before = copy.deepcopy(s)
        with self.assertRaisesRegex(ValueError, 'BLOCK_RESEARCH'):
            registry.freeze(s, c['hypothesis_id'], T+timedelta(days=1), 'commit-a')
        self.assertEqual(s, before)

    def test_promotion_old_eligible_gate_blocked(self):
        s = state(); c = challenger(s); evidence(s, c, 11)
        at = T+timedelta(days=12); g = gate.evaluate_gate(s, at, at)
        block(s, at+timedelta(hours=1)); before = copy.deepcopy(s)
        with self.assertRaisesRegex(ValueError, 'BLOCK_RESEARCH'):
            gate.promote(s, g['gate_id'], c['challenger_id'],
                         {'actor': 'human', 'reason': 'review', 'confirmed_human': True}, at+timedelta(days=1))
        self.assertEqual(s, before)

    def test_final_start_direct_function_blocked(self):
        s, c, f, at = prepared_final(); s['final_confirmations'] = []
        block(s, at+timedelta(hours=1)); before = copy.deepcopy(s)
        with self.assertRaisesRegex(ValueError, 'BLOCK_RESEARCH'):
            final.start(s, c['challenger_id'], '00:00:00', at+timedelta(days=1))
        self.assertEqual(s, before)

    def test_final_decision_direct_function_blocked(self):
        s, c, f, at = prepared_final(); evidence(s, c, 14, 12, at)
        block(s, at+timedelta(days=14))
        final.evaluate(s, f['final_id'], at+timedelta(days=14))
        self.assertEqual(f['final_decision_count'], 0)

    def test_restart_guard_and_readonly_diagnostics(self):
        s, c, f, at = prepared_final(); s['final_confirmations'] = []
        block(s, at+timedelta(hours=1))
        with tempfile.TemporaryDirectory() as d:
            write(d, s); s = read(d)
        before = copy.deepcopy(s)
        validate(s); report(s, at+timedelta(days=1))
        self.assertEqual(s, before)
        with self.assertRaisesRegex(ValueError, 'BLOCK_RESEARCH'):
            final.start(s, c['challenger_id'], '00:00:00', at+timedelta(days=1))


class DiscoveryRegimeRegression(unittest.TestCase):
    def test_input_unchanged_and_repeat_deterministic(self):
        rows = self.rows(); original = copy.deepcopy(rows)
        first = discovery_events({'rows': rows}, T+timedelta(hours=1))
        second = discovery_events({'rows': list(reversed(rows))}, T+timedelta(hours=1))
        self.assertEqual(rows, original); self.assertEqual(first, second)

    def rows(self):
        return [{'strategy': 'PULLBACK', 'scan_time_jst': iso(T+timedelta(minutes=m)),
                 'symbol': s, 'direction': d, 'entry': 100., 'row_hash': str(i)}
                for i, (s, m, d) in enumerate([('A', 0, 'LONG'), ('A', 40, 'LONG'),
                                               ('B', 5, 'SHORT'), ('C', 5, 'SHORT')])]

    def test_chronological_regime_not_symbol_history(self):
        events = discovery_events({'rows': self.rows()}, T+timedelta(hours=1))
        b = next(e for e in events if e['symbol'] == 'B')
        self.assertEqual(b['prior_long_count'], 1)
        self.assertEqual(b['crowding_state'], 'OPPOSITE')

    def test_permutations_match_chronological_oracle(self):
        rows = self.rows()
        rng = random.Random(511)
        variants = [sorted(rows, key=lambda r: r['symbol']), list(reversed(rows))]
        for _ in range(5):
            v = copy.deepcopy(rows); rng.shuffle(v); variants.append(v)
        expected = None
        for rows in variants:
            events = discovery_events({'rows': rows}, T+timedelta(hours=1))
            self.assertEqual([e['scan_bucket_jst'] for e in events], sorted(e['scan_bucket_jst'] for e in events))
            if expected is None: expected = events
            self.assertEqual(events, expected)
            for e in events:
                if e['symbol'] in ('B', 'C'):
                    self.assertEqual(e['prior_long_count'], 1)
                    self.assertEqual(e['prior_short_count'], 0)  # current bucket excluded


if __name__ == '__main__': unittest.main()
