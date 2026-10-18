import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from model511_adaptive.config import dt, iso, digest, base_model, next_bucket, BOOTSTRAP
from model511_adaptive.storage import empty, validate, read, write, export, lock
from model511_adaptive.scheduler import due, daily_cutoff
from model511_adaptive import registry, forward, gate, final_confirmation as final
from model511_adaptive.statistics import summarize, cluster_bootstrap, holm, robustness
from model511_adaptive.shadow import evaluate
from model511_adaptive.runner import run, redact_holdouts
from model511_adaptive.__main__ import main

T = dt('2026-10-01T00:00:00+09:00')
CONFIG = dict(fee_bps_entry=1., fee_bps_exit=1., slippage_bps_entry=1., slippage_bps_exit=1.)


def state(mode='FORWARD'):
    s = empty(T, mode, 'commit-a')
    s['scans'].append({'scan_id': 'SCAN_0', 'scheduled_at_jst': iso(T), 'source_cutoff_jst': iso(T),
                       'data_quality_status': 'PASS'})
    return s


def challenger(s, variant=None, at=T):
    n = len(s['trials'])+1
    tr = registry.trial(s, {'request_id': f't{n}', 'scan_id': 'SCAN_0', 'hypothesis_family': f'fam{n}',
          'features': [], 'parameters': {}, 'dataset_start_jst': iso(T-timedelta(days=5)),
          'dataset_end_jst': iso(T), 'candidate_family_size': 1, 'candidate_index': 1,
          'result_summary': {}, 'selected_for_registration': True, 'selection_reason': 'mechanism',
          'candidate_count_quality': 'EXACT'}, at)
    h = registry.register(s, {'request_id': f'h{n}', 'trial_id': tr['trial_id'], 'parent_model_id': 'BETA1',
          'hypothesis_family': f'fam{n}', 'hypothesis_description': 'replication',
          'economic_or_market_rationale': 'predeclared hypothesis', 'threshold_rationale': 'fixed policy',
          'model_spec_json': variant or base_model(), 'major_concept_change': 'Replication'}, at)
    return registry.freeze(s, h['hypothesis_id'], at, 'commit-a')


def event(at, eid='e', side='LONG', symbol='X_USDT'):
    return {'event_id': eid, 'symbol': symbol, 'direction': side, 'entry_price': 100.,
            'entry_time_jst': iso(at), 'scan_bucket_jst': iso(at), 'source_scan_time': iso(at),
            'source_cutoff_jst': iso(at+timedelta(minutes=5)),
            'decision_created_at_jst': iso(at+timedelta(minutes=5)),
            'has_pullback': True, 'crowding_state': 'OPPOSITE', 'asset_class': 'UNKNOWN',
            'evaluation_mode': 'FORWARD', 'strategy_set': 'PULLBACK', 'bid': 99.99, 'ask': 100.01,
            'ticker_time_jst': iso(at)}


def evidence(s, c, days=30, per_day=20, start=T+timedelta(minutes=5), value=1.):
    # This fixture represents a fully observed interval, including empty buckets.
    # Outcomes alone cannot establish production input completeness.
    # Explicit synthetic source coverage includes two subsequent empty days.
    end = iso(start+timedelta(days=days+2))
    s['source_population_start_jst'] = iso(T)
    s['entry_population_complete_through_jst'] = max(s.get('entry_population_complete_through_jst') or end, end)
    for d in range(days):
        for i in range(per_day):
            at = start+timedelta(days=d, minutes=35*i)
            eid = f'synthetic-{iso(at)}-{i}'
            e = event(at, eid, 'LONG' if i%2 else 'SHORT', f'S{i%5}')
            s['source_history'][eid] = copy.deepcopy(e)
            s['evidence'].append({**e, 'evidence_id': c['challenger_id']+'|'+eid, 'challenger_id': c['challenger_id'],
                'model_spec_hash': c['model_spec_hash'], 'mode': s['mode'], 'decision_at_jst': iso(at+timedelta(minutes=5)),
                'gross_R': value+.1, 'net_R': value, 'spread_R': .04, 'fee_R': .03, 'slippage_R': .03,
                'cost_settings': CONFIG.copy(), 'entry_execution': {'expected_fill_price':100.01},
                'exit_expected_fill_price':100.5, 'exit_quote_timestamp':iso(at+timedelta(minutes=10)),
                'exit_result': 'TP' if value > 0 else 'SL',
                'exit_timestamp': iso(at+timedelta(minutes=10)),
                'reference_resolved_at_jst': iso(at+timedelta(minutes=10)),
                'execution_resolved_at_jst': iso(at+timedelta(minutes=10))})


def beta(s, c, at):
    g = gate.evaluate_gate(s, at, at)
    return gate.promote(s, g['gate_id'], c['challenger_id'], {'actor': 'human', 'reason': 'reviewed', 'confirmed_human': True}, at)


def prepared_final():
    s = state()
    c = challenger(s)
    evidence(s, c)
    s['execution_settings'] = CONFIG.copy()
    at = T+timedelta(days=31)
    beta(s, c, at)
    f = final.start(s, c['challenger_id'], '00:00:00', at)
    return s, c, f, at


class CalendarTests(unittest.TestCase):
    def test_naive_rejected(self):
        with self.assertRaises(ValueError): dt('2026-10-01')

    def test_fixed_calendar_and_catchup(self):
        self.assertEqual(due(T, T+timedelta(days=16), 5), [iso(T+timedelta(days=d)) for d in (5, 10, 15)])

    def test_calendar_idempotent(self):
        self.assertEqual(due(T, T+timedelta(days=5), 5, [iso(T+timedelta(days=5))]), [])

    def test_freeze_next_complete_bucket(self):
        self.assertEqual(next_bucket(T), T+timedelta(minutes=5))
        self.assertEqual(next_bucket(T+timedelta(seconds=1)), T+timedelta(minutes=5))

    def test_daily_delayed_job_uses_fixed_cutoff(self):
        self.assertEqual(daily_cutoff(T, '09:00:00', T+timedelta(days=15, hours=10)), T+timedelta(days=15, hours=9))

    def test_invalid_daily_clock(self):
        with self.assertRaises(ValueError): daily_cutoff(T, '09:00:00+09:00', T)


class IdentityTests(unittest.TestCase):
    def test_model_hash_frozen(self):
        s = state(); c = challenger(s)
        c['model_spec_json']['exit']['rr'] = 2
        with self.assertRaises(ValueError): validate(s)

    def test_spec_hash_mismatch(self):
        s = state(); s['spec_hash'] = 'bad'
        with self.assertRaises(ValueError): validate(s)

    def test_git_provenance_required(self):
        s = state(); c = challenger(s); c['git_commit_hash'] = ''
        with self.assertRaises(ValueError): validate(s)

    def test_changed_model_new_hash_and_clock(self):
        s = state(); c = challenger(s)
        model = base_model(); model['exit']['max_hours'] = 6
        d = challenger(s, model, T+timedelta(days=3))
        self.assertNotEqual(c['model_spec_hash'], d['model_spec_hash'])
        self.assertEqual(d['forward_start_jst'], iso(T+timedelta(days=3, minutes=5)))

    def test_duplicate_hypothesis_not_refrozen(self):
        s = state(); challenger(s)
        with self.assertRaises(ValueError): challenger(s)
        self.assertEqual(s['hypotheses'][-1]['status'], 'DUPLICATE')

    def test_entry_order_canonical(self):
        a = base_model(); b = base_model(); b['entry'].reverse()
        self.assertEqual(digest(registry.normalize_model(a)), digest(registry.normalize_model(b)))

    def test_numeric_spelling_cannot_evade_duplicate_identity(self):
        a = base_model(); b = base_model(); b['exit']['rr'] = 1
        a['entry'].append({'field': 'persistence_score', 'op': 'ge', 'value': 1})
        b['entry'].append({'field': 'persistence_score', 'op': 'ge', 'value': '1.0'})
        self.assertEqual(digest(registry.normalize_model(a)), digest(registry.normalize_model(b)))

    def test_numeric_csv_feature_comparison(self):
        model = base_model(); model['entry'].append({'field': 'funding_rate', 'op': 'eq', 'value': 0})
        e = event(T); e['funding_rate'] = '0.0'
        self.assertTrue(forward.matches(registry.normalize_model(model), e)[0])

    def test_outcome_feature_rejected(self):
        m = base_model(); m['entry'].append({'field': 'net_R', 'op': 'ge', 'value': 0})
        with self.assertRaises(ValueError): registry.normalize_model(m)

    def test_unknown_features_not_invented(self):
        m = base_model(); m['entry'].append({'field': 'future_rsi', 'op': 'ge', 'value': 50})
        with self.assertRaises(ValueError): registry.normalize_model(m)

    def test_exit_grid_frozen(self):
        m = base_model(); m['exit']['sl_pct'] = .4173
        with self.assertRaises(ValueError): registry.normalize_model(m)

    def test_trial_count_unknown(self):
        s = state(); challenger(s); s['trials'][0]['candidate_count_quality'] = 'UNKNOWN'
        self.assertIsNone(registry.candidate_count(s))

    def test_unlogged_losers_unresolved(self):
        s = state(); challenger(s); s['trials'][0]['candidate_family_size'] = 200
        self.assertIsNone(registry.candidate_count(s))

    def test_multi_concept_rejected(self):
        m = base_model(); m['exit']['max_hours'] = 6
        m['entry'].append({'field': 'asset_class', 'op': 'eq', 'value': 'UNKNOWN'})
        with self.assertRaises(ValueError): challenger(state(), m)

    def test_freeze_idempotent(self):
        s = state(); c = challenger(s)
        self.assertEqual(registry.freeze(s, c['hypothesis_id'], T+timedelta(days=1), 'b'), c)
        self.assertEqual(len(s['challengers']), 1)

    def test_rejected_frozen_hypothesis_and_evidence_retained(self):
        s = state(); c = challenger(s); evidence(s, c, 1, 1)
        frozen = copy.deepcopy(c['model_spec_json'])
        registry.reject(s, c['hypothesis_id'], 'Human rejected hypothesis', T+timedelta(days=1))
        self.assertEqual(c['model_spec_json'], frozen)
        self.assertEqual(len(s['evidence']), 1)
        self.assertEqual(c['status'], 'REJECTED')
        self.assertEqual(gate.evaluate_gate(s, T+timedelta(days=15), T+timedelta(days=15))['candidates'], [])


class EvidenceTests(unittest.TestCase):
    def test_discovery_excluded_and_membership_idempotent(self):
        s = state(); c = challenger(s)
        e = event(T+timedelta(minutes=5))
        source = {'evaluation_mode': 'FORWARD', 'events': [event(T, 'old'), e]}
        forward.update(s, source, {}, T+timedelta(minutes=10))
        forward.update(s, source, {}, T+timedelta(minutes=10))
        self.assertEqual(len(s['memberships']), 1)
        self.assertEqual(len(s['evidence']), 1)

    def test_backfill_cannot_feed_forward(self):
        s = state(); challenger(s)
        with self.assertRaises(ValueError): forward.update(s, {'evaluation_mode': 'BACKFILL', 'events': []}, {}, T)

    def test_late_event_does_not_rewrite(self):
        s = state(); challenger(s); e = event(T+timedelta(minutes=5))
        source = {'evaluation_mode': 'FORWARD', 'events': [e]}
        forward.update(s, source, {}, T+timedelta(minutes=10))
        e['has_pullback'] = False
        forward.update(s, source, {}, T+timedelta(minutes=15))
        self.assertTrue(s['memberships'][0]['eligible'])
        self.assertEqual(s['audit'][-1]['action'], 'LATE_EVENT_CHANGE_IGNORED')

    def test_missing_feature_ineligible(self):
        e = event(T); del e['has_pullback']
        self.assertFalse(forward.matches(base_model(), e)[0])

    def test_asset_name_not_classified(self):
        s = state(); challenger(s)
        source = {'evaluation_mode': 'FORWARD', 'events': [event(T+timedelta(minutes=5), symbol='GOLD_USDT')]}
        forward.update(s, source, {}, T+timedelta(minutes=10))
        self.assertEqual(s['evidence'][0]['asset_class'], 'UNKNOWN')

    def test_asof_does_not_see_later_result(self):
        s = state(); c = challenger(s); evidence(s, c, 1, 1)
        rows = forward.cohort(s, c['challenger_id'], T+timedelta(minutes=12))
        self.assertIsNone(rows[0]['gross_R'])
        self.assertEqual(s['evidence'][0]['gross_R'], 1.1)

    def test_membership_cutoff_half_open(self):
        s = state(); c = challenger(s); evidence(s, c, 1, 1)
        self.assertEqual(forward.cohort(s, c['challenger_id'], T+timedelta(minutes=5)), [])

    def test_beta_carries_cumulative_separate_post_beta(self):
        s = state(); c = challenger(s); evidence(s, c, 11)
        at = T+timedelta(days=12); b = beta(s, c, at)
        self.assertEqual(len(forward.cohort(s, c['challenger_id'], at)), 220)
        self.assertEqual(forward.cohort(s, c['challenger_id'], at, b['promoted_at_jst']), [])
        self.assertEqual(b['cumulative_forward_start_jst'], c['forward_start_jst'])


class StatisticsTests(unittest.TestCase):
    def test_directions_same_cluster(self):
        s = state(); c = challenger(s); evidence(s, c, 1, 2)
        s['evidence'][1]['entry_time_jst'] = s['evidence'][0]['entry_time_jst']
        m = summarize(s['evidence'])
        self.assertEqual(m['time_cluster_30m_n'], 1)
        self.assertEqual(m['direction_cluster_30m_n'], 2)
        self.assertIsNone(m['bootstrap_30m']['ci'])

    def test_bootstrap_reproducible(self):
        s = state(); c = challenger(s); evidence(s, c, 2)
        self.assertEqual(cluster_bootstrap(s['evidence']), cluster_bootstrap(s['evidence']))

    def test_missing_net_not_gross(self):
        s = state(); c = challenger(s); evidence(s, c, 1, 2)
        s['evidence'][0]['net_R'] = None
        m = summarize(s['evidence'])
        self.assertIsNone(m['net_ev']); self.assertEqual(m['net_coverage_ratio'], .5)

    def test_unknown_not_pending(self):
        s = state(); c = challenger(s); evidence(s, c, 1, 1)
        s['evidence'][0].update(exit_result='UNKNOWN', gross_R=None, net_R=None)
        m = summarize(s['evidence'])
        self.assertEqual(m['pending_reference_n'], 0); self.assertEqual(m['resolved_gross_n'], 0)

    def test_duplicate_events_not_samples(self):
        s = state(); c = challenger(s); evidence(s, c, 1, 1)
        with self.assertRaises(ValueError): summarize(s['evidence']*60)

    def test_holm_all_candidates(self):
        self.assertEqual(holm({'a': .01, 'b': .04}, 10)['adjusted'], {'a': .1, 'b': .36})
        self.assertEqual(holm({'a': .01}, None)['status'], 'MULTIPLE_TESTING_UNRESOLVED')

    def test_robustness_diagnostic_no_blocks(self):
        s = state(); c = challenger(s); evidence(s, c, 2)
        d = robustness(s['evidence'], T)
        self.assertIn('leave_one_symbol_out_ev', d['net_R'])
        self.assertNotIn('blocks', d)


class GateTests(unittest.TestCase):
    def test_eligible_not_auto_promoted(self):
        s = state(); c = challenger(s); evidence(s, c, 11)
        g = gate.evaluate_gate(s, T+timedelta(days=12), T+timedelta(days=12))
        self.assertEqual(g['decision'], 'NO_CHANGE')
        self.assertEqual(g['candidates'][0]['status'], 'ELIGIBLE_FOR_PROMOTION_REVIEW')
        self.assertEqual(s['betas'], [])

    def test_no_human_no_promotion(self):
        s = state(); c = challenger(s); evidence(s, c, 11)
        g = gate.evaluate_gate(s, T+timedelta(days=12), T+timedelta(days=12))
        with self.assertRaises(ValueError): gate.promote(s, g['gate_id'], c['challenger_id'], {}, T+timedelta(days=12))

    def test_insufficient_cannot_promote(self):
        s = state(); c = challenger(s); evidence(s, c, 1, 1)
        with self.assertRaises(ValueError): beta(s, c, T+timedelta(days=12))

    def test_unknown_trials_block(self):
        s = state(); c = challenger(s); evidence(s, c, 11)
        s['trials'][0]['candidate_count_quality'] = 'UNKNOWN'
        with self.assertRaises(ValueError): beta(s, c, T+timedelta(days=12))

    def test_data_block_cannot_override(self):
        s = state(); c = challenger(s); evidence(s, c, 11)
        g = gate.evaluate_gate(s, T+timedelta(days=12), T+timedelta(days=12), [{'level': 'BLOCK', 'reason': 'CORRUPT'}])
        self.assertEqual(g['candidates'][0]['status'], 'INVALID_DATA')

    def test_max_one_beta_per_gate(self):
        s = state(); c = challenger(s); evidence(s, c, 11)
        m = base_model(); m['exit']['rr'] = 2
        d = challenger(s, m); evidence(s, d, 11, value=2)
        at = T+timedelta(days=12)
        b = beta(s, c, at)  # lower EV human choice remains valid
        with self.assertRaises(ValueError): gate.promote(s, b['gate_id'], d['challenger_id'], {'actor': 'human', 'reason': 'reviewed', 'confirmed_human': True}, at)

    def test_gate_reexecution_no_history_growth(self):
        s = state(); c = challenger(s); evidence(s, c, 11)
        at = T+timedelta(days=12)
        gate.evaluate_gate(s, at, at); gate.evaluate_gate(s, at, at)
        self.assertEqual(c['evaluation_count'], 1)

    def test_fee_block_before_practice(self):
        s = state(); c = challenger(s); evidence(s, c)
        beta(s, c, T+timedelta(days=31))
        self.assertIn('EXECUTION_COST_CONFIG_BLOCKER', gate.practice(s, c['challenger_id'], T+timedelta(days=31))['blocks'])

    def test_gate_does_not_borrow_practice_net_coverage_threshold(self):
        s = state(); c = challenger(s); evidence(s, c, 11)
        s['evidence'][0]['net_R'] = None
        at = T+timedelta(days=12)
        g = gate.evaluate_gate(s, at, at)
        candidate = g['candidates'][0]
        self.assertEqual(candidate['status'], 'ELIGIBLE_FOR_PROMOTION_REVIEW')
        self.assertIsNone(candidate['metrics']['net_ev'])
        self.assertLess(candidate['metrics']['net_coverage_ratio'], 1)
        self.assertEqual(g['decision'], 'NO_CHANGE')


class FinalTests(unittest.TestCase):
    def test_final_start_requires_cost_config(self):
        s = state(); c = challenger(s); evidence(s, c); beta(s, c, T+timedelta(days=31))
        with self.assertRaises(ValueError): final.start(s, c['challenger_id'], '00:00:00', T+timedelta(days=31))

    def test_pending_reference_cannot_pass(self):
        s, c, f, at = prepared_final()
        evidence(s, c, 14, 12, at)
        s['evidence'][-1].update(exit_result=None, gross_R=None, net_R=None, reference_resolved_at_jst=None)
        final.evaluate(s, f['final_id'], at+timedelta(days=14))
        self.assertEqual(f['status'], 'FINAL_INSUFFICIENT_DATA')
        self.assertEqual(f['final_decision_count'], 0)
        self.assertIn('PENDING_REFERENCE_TRADES', f['evaluations'][-1]['blocks'])

    def test_one_formal_pass_and_readonly_repeat(self):
        s, c, f, at = prepared_final(); evidence(s, c, 14, 12, at)
        final.evaluate(s, f['final_id'], at+timedelta(days=14))
        self.assertEqual(f['final_decision'], 'PASS')
        sealed = copy.deepcopy(f)
        final.evaluate(s, f['final_id'], at+timedelta(days=15))
        self.assertEqual(f, sealed)
        self.assertEqual(f['notification']['type'], 'PRACTICE_CANDIDATE_READY')

    def test_fail_never_extended_for_profit(self):
        s, c, f, at = prepared_final(); evidence(s, c, 14, 12, at, value=-1)
        final.evaluate(s, f['final_id'], at+timedelta(days=14))
        self.assertEqual(f['final_decision'], 'FAIL')
        evidence(s, c, 14, 12, at+timedelta(days=15), value=100)
        final.evaluate(s, f['final_id'], at+timedelta(days=30))
        self.assertEqual(f['final_decision'], 'FAIL')
        self.assertEqual(len(f['evaluations']), 14)

    def test_same_day_not_reevaluated(self):
        s, c, f, at = prepared_final()
        final.evaluate(s, f['final_id'], at+timedelta(days=14, hours=1))
        final.evaluate(s, f['final_id'], at+timedelta(days=14, hours=10))
        self.assertEqual(len(f['evaluations']), 14)

    def test_final_population_excludes_prefinal_and_cutoff(self):
        s, c, f, at = prepared_final(); evidence(s, c, 14, 12, at)
        evidence(s, c, 1, 1, at+timedelta(days=14))
        final.evaluate(s, f['final_id'], at+timedelta(days=14))
        self.assertEqual(len(f['evaluations'][-1]['evaluated_event_ids']), 168)

    def test_outage_replays_first_ready_cutoff_without_future_data(self):
        s, c, f, at = prepared_final(); evidence(s, c, 16, 12, at)
        final.evaluate(s, f['final_id'], at+timedelta(days=16))
        self.assertEqual(f['evaluation_cutoff_jst'], iso(at+timedelta(days=14)))
        self.assertEqual(f['evaluations'][-1]['evaluated_at_jst'], iso(at+timedelta(days=16)))
        self.assertEqual(len(f['sealed_evidence']), 168)

    def test_final_holding_not_forced_three(self):
        s = state(); m = base_model(); m['exit']['max_hours'] = 24
        c = challenger(s, m); evidence(s, c); s['execution_settings'] = CONFIG.copy()
        at = T+timedelta(days=31); beta(s, c, at)
        f = final.start(s, c['challenger_id'], '00:00:00', at)
        self.assertEqual(f['frozen_max_hours'], 24)

    def test_final_not_selection_evidence(self):
        s, c, f, at = prepared_final(); evidence(s, c, 14, 12, at)
        self.assertEqual(len(forward.cohort(s, c['challenger_id'], at+timedelta(days=14))), 600)

    def test_same_model_final_retry_forbidden(self):
        s, c, f, at = prepared_final(); evidence(s, c, 14, 12, at, -1)
        final.evaluate(s, f['final_id'], at+timedelta(days=14))
        with self.assertRaises(ValueError): final.start(s, c['challenger_id'], '00:00:00', at+timedelta(days=15))


class StorageTests(unittest.TestCase):
    def test_restart_and_projection_rebuild(self):
        s = state(); challenger(s)
        with tempfile.TemporaryDirectory() as d:
            with lock(d): write(d, s)
            path = Path(d)/'challenger_registry.csv'; original = path.read_bytes(); path.unlink()
            loaded = read(d); export(d, loaded)
            self.assertEqual(loaded, s); self.assertEqual(path.read_bytes(), original)

    def test_corruption_never_resets(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'adaptive_state.json').write_text('{')
            with self.assertRaises(ValueError): read(d)

    def test_atomic_failure_preserves_old_state(self):
        s = state()
        with tempfile.TemporaryDirectory() as d:
            write(d, s); before = (Path(d)/'adaptive_state.json').read_bytes()
            s['revision'] = 2
            with patch('model511.storage.os.replace', side_effect=OSError('disk')):
                with self.assertRaises(OSError): write(d, s)
            self.assertEqual((Path(d)/'adaptive_state.json').read_bytes(), before)

    def test_projection_failure_state_committed(self):
        s = state()
        with tempfile.TemporaryDirectory() as d:
            with patch('model511_adaptive.storage.export', side_effect=OSError('disk')):
                with self.assertRaises(OSError): write(d, s)
            self.assertEqual(read(d), s)

    def test_forward_asof_forbidden(self):
        with self.assertRaises(SystemExit): main(['scan', '--as-of', iso(T)])


if __name__ == '__main__': unittest.main()
