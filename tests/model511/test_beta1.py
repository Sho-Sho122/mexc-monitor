import copy
import csv
import importlib.util
import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from model511.config import dt, iso, bucket, SPEC_HASH
from model511.unique_engine import unique_events
from model511.regime_engine import regime
from model511.model_league import membership, CORE
from model511.shadow_engine import evaluate
from model511.execution import costs, entry_execution
from model511.runner import process, settle
from model511.storage import empty, write, read, export, lock
from model511.source_reader import read_snapshot
from model511.evaluation import summarize, reports

T = dt("2026-10-01T10:02:00+09:00")


def signal(strategy="PULLBACK", time=T, symbol="SOL_USDT", side="LONG", price=100, key=None):
    return {"strategy": strategy, "scan_time_jst": iso(time), "symbol": symbol,
            "direction": side, "entry": price, "row_hash": key or strategy+iso(time)+symbol+side,
            "source_file": "strategy_"+strategy.lower()+".csv",
            "bid": "99.99", "ask": "100.01", "ticker_time_jst": iso(time),
            "spread_pct": "0.02"}


def event(time=T, side="LONG"):
    e = unique_events([signal(time=time, side=side)])[0][0]
    e.update(decision_created_at_jst=iso(bucket(time)+timedelta(minutes=5)),
             source_cutoff_jst=iso(bucket(time)+timedelta(minutes=5)))
    return e


def bar(start, end=None, high=100.1, low=99.9):
    return {"start": iso(start), "end": iso(end or (dt(start)+timedelta(minutes=5))),
            "high": high, "low": low, "close": 100, "open": 100}


def history_to_boundary(time=T):
    start = bucket(time)
    if start < time:
        start += timedelta(minutes=5)
    deadline = time+timedelta(hours=3)
    end = bucket(deadline)
    out = []
    while start < end:
        out.append(bar(start))
        start += timedelta(minutes=5)
    return out


def market_case(high=100.1, low=99.9, fine=(), time=T):
    deadline = time+timedelta(hours=3)
    bars = history_to_boundary(time)+[bar(bucket(deadline), high=high, low=low)]
    quote = {"timestamp": iso(deadline+timedelta(seconds=1)), "price": 100.2,
             "bid": 100.19, "ask": 100.21, "source": "RECORDED_TICKER"}
    return bars, [quote], deadline+timedelta(minutes=5), fine


class UniqueTests(unittest.TestCase):
    def test_four_strategies_one_event(self):
        rows = [signal(s) for s in ("PULLBACK", "OI_FUNDING", "HIGH_EDGE", "TREND_VOLUME")]
        es, _ = unique_events(rows)
        self.assertEqual(len(es), 1)
        self.assertEqual(es[0]["strategy_count"], 4)

    def test_long_short_separate(self):
        self.assertEqual(len(unique_events([signal(), signal(side="SHORT")])[0]), 2)

    def test_id_is_deterministic(self):
        a = unique_events([signal(), signal("HIGH_EDGE")])[0]
        b = unique_events([signal("HIGH_EDGE"), signal()])[0]
        self.assertEqual(a, b)

    def test_pullback_reference_and_duplicate_audit(self):
        rows = [signal("HIGH_EDGE", T-timedelta(seconds=2), price=90),
                signal(price=100), signal(time=T+timedelta(seconds=1), price=110)]
        es, audit = unique_events(rows)
        self.assertEqual(es[0]["entry_price"], 100)
        self.assertEqual(audit[0]["reason"], "DUPLICATE_PULLBACK")

    def test_non_pullback_earliest(self):
        es, _ = unique_events([signal("OI_FUNDING", price=110),
                              signal("HIGH_EDGE", T-timedelta(seconds=1), price=90)])
        self.assertEqual(es[0]["entry_price"], 90)

    def test_non_pullback_tie_fixed(self):
        rows = [signal("OI_FUNDING", price=110), signal("HIGH_EDGE", price=90)]
        self.assertEqual(unique_events(rows)[0], unique_events(rows[::-1])[0])

    def test_jst_bucket_and_offset(self):
        self.assertEqual(bucket("2026-10-01T01:04:59Z"), dt("2026-10-01T10:00:00+09:00"))
        self.assertNotEqual(bucket(T), bucket(T+timedelta(minutes=3)))

    def test_missing_context_does_not_remove_core(self):
        e = event()
        e.update(crowding_state="OPPOSITE", persistence_category="HIGH", asset_class="UNKNOWN")
        self.assertTrue(next(m["eligible"] for m in membership(e) if m["model_id"] == CORE))

    def test_all_model_conditions(self):
        e = event()
        e.update(crowding_state="OPPOSITE", persistence_category="LOW", asset_class="CRYPTO_NATIVE")
        result = {m["model_id"]: m["eligible"] for m in membership(e)}
        self.assertEqual(len(result), 11)
        self.assertTrue(result[CORE])
        self.assertTrue(all(v for k, v in result.items() if k.startswith("CH_")))
        self.assertFalse(result["CTRL_OPPOSITE_NON_PULLBACK"])
        self.assertFalse(result["CTRL_PULLBACK_MATCH"])

    def test_core_has_no_extra_filters(self):
        e = event(side="SHORT")
        e.update(crowding_state="OPPOSITE", persistence_category="HIGH",
                 asset_class="EQUITY_ETF_LINKED", spread_pct=100, btc_ret_5m_pct=-999,
                 funding_rate=99, rsi5=0)
        self.assertTrue(membership(e)[0]["eligible"])


class RegimeTests(unittest.TestCase):
    def prior(self, minutes, side="LONG", symbol="A"):
        return event(T-timedelta(minutes=minutes), side) | {
            "event_id": symbol+str(minutes)+side, "symbol": symbol}

    def test_current_bucket_excluded(self):
        out = regime(event(), [self.prior(0), self.prior(5, "SHORT")])
        self.assertEqual((out["prior_long_count"], out["prior_short_count"]), (0, 1))

    def test_only_previous_six_buckets(self):
        out = regime(event(), [self.prior(m) for m in (5, 10, 15, 20, 25, 30, 35)])
        self.assertEqual(out["prior_long_count"], 6)
        self.assertEqual(out["persistence_score"], 6)

    def test_match(self):
        self.assertEqual(regime(event(), [self.prior(5)])["crowding_state"], "MATCH")

    def test_opposite(self):
        self.assertEqual(regime(event(side="SHORT"), [self.prior(5)])["crowding_state"], "OPPOSITE")

    def test_tie_neutral(self):
        out = regime(event(), [self.prior(5), self.prior(5, "SHORT")])
        self.assertEqual(out["crowding_state"], "NEUTRAL")

    def test_empty_neutral_and_zero_ratio(self):
        out = regime(event(), [])
        self.assertEqual(out["majority_direction"], "NEUTRAL")
        self.assertEqual(out["majority_ratio"], 0)

    def test_dedup_and_persistence_categories(self):
        for n, category in ((1, "LOW"), (3, "MID"), (5, "HIGH")):
            hist = [self.prior(5*i) for i in range(1, n+1)]
            out = regime(event(), hist+hist)
            self.assertEqual(out["prior_long_count"], n)
            self.assertEqual(out["persistence_category"], category)
            self.assertTrue(0 <= out["persistence_score"] <= 6)


class ExitTests(unittest.TestCase):
    def run_case(self, high=100.1, low=99.9, fine=(), time=T):
        bars, quotes, now, _ = market_case(high, low, fine, time)
        return evaluate(event(time), bars, quotes, now, fine)

    def test_A_boundary_no_touch_TIME(self):
        out = self.run_case()
        self.assertEqual(out["exit_result"], "TIME")
        self.assertAlmostEqual(out["gross_R"], 0.4)
        self.assertEqual(out["exit_price_source"], "RECORDED_TICKER")

    def test_B_boundary_TP_unknown(self):
        out = self.run_case(high=100.6)
        self.assertEqual(out["exit_result"], "UNKNOWN")
        self.assertEqual(out["ambiguous_deadline_boundary"], 1)
        self.assertEqual(out["unknown_reason"], "DEADLINE_BOUNDARY_INSUFFICIENT_GRANULARITY")

    def test_C_boundary_SL_unknown(self):
        self.assertEqual(self.run_case(low=99.4)["exit_result"], "UNKNOWN")

    def test_D_boundary_both_unknown(self):
        self.assertEqual(self.run_case(high=100.6, low=99.4)["exit_result"], "UNKNOWN")

    def test_E_fine_before_deadline_TP(self):
        start = T+timedelta(hours=3)-timedelta(minutes=2)
        fine = [bar(start, start+timedelta(minutes=1), high=100.6)]
        self.assertEqual(self.run_case(high=100.6, fine=fine)["exit_result"], "TP")

    def test_F_fine_after_deadline_only_TIME(self):
        start = T+timedelta(hours=3)-timedelta(minutes=2)
        fine = [bar(start+timedelta(minutes=i), start+timedelta(minutes=i+1),
                    high=100.6 if i >= 2 else 100.1) for i in range(5)]
        self.assertEqual(self.run_case(high=100.6, fine=fine)["exit_result"], "TIME")

    def test_G_observation_before_deadline_rejected(self):
        bars, quotes, now, _ = market_case()
        early = {**quotes[0], "timestamp": iso(T+timedelta(hours=3)-timedelta(microseconds=1)), "price": 90}
        out = evaluate(event(), bars, [early]+quotes, now)
        self.assertEqual(out["exit_reference_price"], 100.2)

    def test_G_observation_exact_deadline_accepted(self):
        bars, quotes, now, _ = market_case()
        quotes[0]["timestamp"] = iso(T+timedelta(hours=3))
        self.assertEqual(evaluate(event(), bars, quotes, now)["exit_result"], "TIME")

    def test_exact_five_min_deadline(self):
        start = bucket(T)
        bars = [bar(start+timedelta(minutes=5*i)) for i in range(36)]
        q = {"timestamp": iso(start+timedelta(hours=3)), "price": 100, "source": "TICK"}
        self.assertEqual(evaluate(event(start), bars, [q], dt(q["timestamp"]))["exit_result"], "TIME")

    def test_intrabar_unknown(self):
        bars = [bar(bucket(T)+timedelta(minutes=5), high=100.6, low=99.4)]
        out = evaluate(event(), bars, [], T+timedelta(minutes=10))
        self.assertEqual(out["ambiguous_intrabar"], 1)
        self.assertIsNone(out["gross_R"])

    def test_fixed_long_short_exits(self):
        b = bar(bucket(T)+timedelta(minutes=5), high=100.6)
        self.assertAlmostEqual(evaluate(event(), [b], [], dt(b["end"]))["gross_R"], 1)
        self.assertAlmostEqual(evaluate(event(side="SHORT"), [b], [], dt(b["end"]))["gross_R"], -1)

    def test_unclosed_bar_not_used(self):
        b = bar(bucket(T)+timedelta(minutes=5), high=100.6)
        self.assertIsNone(evaluate(event(), [b], [], dt(b["end"])-timedelta(seconds=1)))

    def test_gap_not_filled(self):
        bars, quotes, now, _ = market_case()
        self.assertIsNone(evaluate(event(), bars[1:], quotes, now))

    def test_no_time_quote_no_earlier_close_substitution(self):
        bars, quotes, now, _ = market_case()
        self.assertIsNone(evaluate(event(), bars, [], now))

    def test_unknown_keeps_audit_price(self):
        out = self.run_case(high=100.6)
        self.assertEqual(out["time_exit_audit_price"], 100.2)
        self.assertIsNone(out["exit_reference_price"])


class CostTests(unittest.TestCase):
    def setUp(self):
        self.e = event()
        self.e.update(bid=99.9, ask=100.1)
        self.x = entry_execution(self.e)
        self.r = {"gross_R": 1.0, "exit_reference_price": 100.5, "exit_timestamp": iso(T+timedelta(hours=3))}
        self.q = {"timestamp": self.r["exit_timestamp"], "bid": 100.4, "ask": 100.6}
        self.c = {"fee_bps_entry": 1, "fee_bps_exit": 2,
                  "slippage_bps_entry": 3, "slippage_bps_exit": 4}

    def test_missing_fee_null(self):
        result = costs(self.e, self.r, self.x, {}, self.q)
        self.assertIsNone(result["net_R"])
        self.assertEqual(result["cost_status"], "MISSING_FEE_CONFIG")

    def test_missing_slippage_null(self):
        del self.c["slippage_bps_exit"]
        self.assertIsNone(costs(self.e, self.r, self.x, self.c, self.q)["net_R"])

    def test_missing_exit_quote_null(self):
        self.assertIsNone(costs(self.e, self.r, self.x, self.c)["net_R"])

    def test_each_cost_once_both_methods(self):
        a = costs(self.e, self.r, self.x, self.c, self.q, "components")
        b = costs(self.e, self.r, self.x, self.c, self.q, "fills")
        self.assertAlmostEqual(a["spread_R"], 0.4)
        expected = 1-0.4-(100.1*1+100.4*2)/10000/0.5-(100.1*3+100.4*4)/10000/0.5
        self.assertAlmostEqual(a["net_R"], expected)
        self.assertAlmostEqual(b["net_R"], expected)

    def test_short_uses_bid_entry_ask_exit(self):
        e = self.e | {"direction": "SHORT"}
        r = self.r | {"exit_reference_price": 99.5}
        q = self.q | {"bid": 99.4, "ask": 99.6}
        x = entry_execution(e)
        self.assertEqual(x["expected_fill_price"], 99.9)
        a = costs(e, r, x, self.c, q)
        self.assertAlmostEqual(a["spread_R"], 0.4)

    def test_fee_increment_once(self):
        a = costs(self.e, self.r, self.x, self.c, self.q)
        self.c["fee_bps_entry"] += 1
        b = costs(self.e, self.r, self.x, self.c, self.q)
        self.assertAlmostEqual(a["net_R"]-b["net_R"], 100.1/10000/0.5)


class StateTests(unittest.TestCase):
    def state(self, mode="BACKFILL"):
        return empty(mode, "fixture_commit")

    def run_once(self, state=None, rows=None, when=None):
        return process(state or self.state(), rows or [signal()], [], [],
                       when or T+timedelta(minutes=3), "fixture_commit")

    def test_same_input_no_duplicate_event_trade(self):
        a = self.run_once()
        b = self.run_once(a)
        self.assertEqual(a["events"], b["events"])
        self.assertEqual(len(b["trades"]), 1)
        self.assertEqual(len(b["open_shadow_positions"]), 1)

    def test_bucket_only_after_end(self):
        a = self.run_once(when=T+timedelta(minutes=2, seconds=59))
        self.assertEqual(len(a["events"]), 0)
        b = self.run_once(a, when=T+timedelta(minutes=3))
        self.assertEqual(len(b["events"]), 1)

    def test_late_signal_does_not_rewrite(self):
        a = self.run_once()
        b = self.run_once(a, [signal(), signal("HIGH_EDGE")], T+timedelta(minutes=8))
        self.assertEqual(a["events"], b["events"])
        self.assertEqual(a["memberships"], b["memberships"])
        self.assertTrue(any(x["reason"] == "LATE_ARRIVING_DATA" for x in b["audit"]))

    def test_late_entire_event_not_added(self):
        a = self.run_once()
        b = self.run_once(a, [signal(), signal(symbol="OTHER")], T+timedelta(minutes=8))
        self.assertEqual(len(b["events"]), 1)

    def test_forward_start_next_complete_bucket(self):
        a = self.run_once(self.state("FORWARD"), when=T)
        self.assertEqual(a["forward_start_jst"], iso(bucket(T)+timedelta(minutes=5)))
        self.assertEqual(a["events"], [])
        rows = [signal(time=T+timedelta(minutes=5))]
        b = self.run_once(a, rows, T+timedelta(minutes=8))
        self.assertEqual(b["events"][0]["evaluation_mode"], "FORWARD")
        self.assertEqual(b["events"][0]["evaluation_phase"], "MAIN_FORWARD")

    def test_restart_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.run_once()
            write(d, a)
            b = self.run_once(read(d, "BACKFILL", "fixture_commit"))
            self.assertEqual(a["events"], b["events"])
            self.assertEqual(a["open_shadow_positions"], b["open_shadow_positions"])

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "511_state.json").write_text("{broken")
            with self.assertRaises(json.JSONDecodeError):
                read(d, "FORWARD", "fixture_commit")

    def test_mode_separation(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, self.state())
            with self.assertRaises(ValueError):
                read(d, "FORWARD", "fixture_commit")

    def test_spec_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.state()
            s["spec_hash"] = "different"
            write(d, s)
            with self.assertRaises(ValueError):
                read(d, "BACKFILL", "fixture_commit")

    def test_csv_projection_recovers_after_crash(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.run_once()
            with patch("model511.storage.export", side_effect=OSError("simulated crash")):
                with self.assertRaises(OSError):
                    write(d, a)
            recovered = read(d, "BACKFILL", "fixture_commit")
            export(d, recovered)
            with Path(d, "511_unique_events.csv").open(encoding="utf-8-sig") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 1)

    def test_second_writer_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with lock(d):
                with self.assertRaises(FileExistsError):
                    with lock(d):
                        pass

    def test_shared_path_one_result_multiple_models(self):
        a = self.run_once()
        bars, quotes, now, _ = market_case()
        market = {"SOL_USDT": {"bars": bars, "observations": quotes}}
        b = settle(a, market, now)
        c = settle(b, market, now)
        self.assertEqual(len(c["results"]), 1)
        self.assertEqual(len(c["open_shadow_positions"]), 0)
        self.assertEqual(b["results"], c["results"])

    def test_unknown_never_reclassified_to_time(self):
        a = self.run_once()
        bars, quotes, now, _ = market_case(high=100.6)
        b = settle(a, {"SOL_USDT": {"bars": bars, "observations": quotes}}, now)
        bars, quotes, now, _ = market_case()
        c = settle(b, {"SOL_USDT": {"bars": bars, "observations": quotes}}, now)
        self.assertEqual(c["results"][0]["exit_result"], "UNKNOWN")

    def test_backwards_cutoff_rejected(self):
        a = self.run_once()
        with self.assertRaises(ValueError):
            self.run_once(a, when=T)


class InputTests(unittest.TestCase):
    def sources(self, root, bad=None):
        for strategy in ("HIGH_EDGE", "OI_FUNDING", "PULLBACK", "TREND_VOLUME"):
            path = Path(root, "strategy_"+strategy.lower()+".csv")
            row = {"event": "OPEN", "symbol": "SOL_USDT", "direction": "LONG",
                   "entry": 100, "scan_time_jst": iso(T)}
            if strategy == "PULLBACK" and bad:
                row.update(bad)
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=row)
                writer.writeheader()
                writer.writerow(row)

    def test_snapshot_manifest_and_four_sources(self):
        with tempfile.TemporaryDirectory() as d:
            self.sources(d)
            rows, manifest, audit = read_snapshot(d, T)
            self.assertEqual(len(rows), 4)
            self.assertEqual(len(manifest), 4)
            self.assertEqual(audit, [])

    def test_invalid_time_audited(self):
        with tempfile.TemporaryDirectory() as d:
            self.sources(d, {"scan_time_jst": ""})
            rows, _, audit = read_snapshot(d, T)
            self.assertEqual(len(rows), 3)
            self.assertEqual(audit[0]["reason"], "INVALID_SCAN_TIME")

    def test_missing_source_is_not_zero_signals(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                read_snapshot(d, T)

    def test_invalid_direction_audited(self):
        with tempfile.TemporaryDirectory() as d:
            self.sources(d, {"direction": "NEUTRAL"})
            self.assertEqual(read_snapshot(d, T)[2][0]["reason"], "INVALID_SYMBOL_OR_DIRECTION")

    def test_nan_entry_audited(self):
        with tempfile.TemporaryDirectory() as d:
            self.sources(d, {"entry": "nan"})
            self.assertEqual(read_snapshot(d, T)[2][0]["reason"], "INVALID_ENTRY")

    def test_future_row_quarantined(self):
        with tempfile.TemporaryDirectory() as d:
            self.sources(d, {"scan_time_jst": iso(T+timedelta(minutes=5))})
            self.assertEqual(read_snapshot(d, T)[2][0]["reason"], "FUTURE_SCAN_TIME")


class ReportTests(unittest.TestCase):
    def row(self, i=0, gross=1, net=None):
        return {"event_id": str(i), "gross_R": gross, "net_R": net, "exit_result": "TP",
                "cluster_30m_id": str(i), "cluster_5m_id": str(i),
                "date": str(i//10), "exit_timestamp": iso(T+timedelta(minutes=i))}

    def test_unknown_excluded_and_reported(self):
        out = summarize([self.row(), self.row(1, None) | {
            "exit_result": "UNKNOWN", "ambiguous_deadline_boundary": 1}])
        self.assertEqual(out["resolved_N"], 1)
        self.assertEqual(out["gross_EV"], 1)
        self.assertEqual(out["UNKNOWN_ratio"], 0.5)
        self.assertEqual(out["ambiguous_deadline_boundary_N"], 1)

    def test_small_sample_insufficient(self):
        self.assertEqual(summarize([self.row()])["qualification"], "INSUFFICIENT_DATA")

    def test_missing_net_never_negative_or_supported(self):
        out = summarize([self.row(i, -1) for i in range(100)])
        self.assertEqual(out["qualification"], "INCONCLUSIVE")
        self.assertIsNone(out["net_EV"])

    def test_negative(self):
        out = summarize([self.row(i, -1, -1.1) for i in range(100)])
        self.assertEqual(out["qualification"], "NEGATIVE")

    def test_supported_with_positive_cluster_ci(self):
        out = summarize([self.row(i, 1, 0.8) for i in range(100)])
        self.assertEqual(out["qualification"], "FORWARD_SUPPORTED")

    def test_too_few_clusters_insufficient(self):
        out = summarize([self.row(i, 1, 0.8) | {"cluster_30m_id": "one"} for i in range(100)])
        self.assertEqual(out["qualification"], "INSUFFICIENT_DATA")


if __name__ == "__main__":
    unittest.main()

