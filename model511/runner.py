import copy
import hashlib
import json
from collections import defaultdict
from datetime import timedelta
from .config import bucket, dt, iso, phase
from .unique_engine import unique_events
from .regime_engine import regime
from .model_league import CORE, membership
from .execution import entry_execution, costs
from .shadow_engine import evaluate, prices
from .evaluation import reports


def audit_once(state, items):
    known = {json.dumps(r, sort_keys=True) for r in state["audit"]}
    for item in items:
        encoded = json.dumps(item, sort_keys=True)
        if encoded not in known:
            state["audit"].append(item)
            known.add(encoded)


def process(state, rows, manifest, source_audit, cutoff, commit, asset_map=None,
            market=None, settings=None, slices=()):
    """Pure transaction: mutate a copy; caller commits only after successful completion."""
    state = copy.deepcopy(state)
    now = dt(cutoff)
    if state["last_cutoff_jst"] and now < dt(state["last_cutoff_jst"]):
        raise ValueError("Cutoff moved backwards")
    first_run = state["last_processed_bucket"] is None
    if first_run and state["evaluation_mode"] == "FORWARD":
        state["forward_start_jst"] = iso(bucket(now) + timedelta(minutes=5))
    end_bucket = bucket(now) - timedelta(minutes=5)
    old_watermark = dt(state["last_processed_bucket"]) if state["last_processed_bucket"] else None
    seen = set(state["seen_rows"])
    for issue in source_audit:
        if issue["reason"] == "FUTURE_SCAN_TIME":
            continue
        timestamp = issue.get("scan_time_jst") or issue.get("source_time")
        try:
            affected = dt(timestamp)
        except (ValueError, TypeError):
            affected = None
        if (state["evaluation_mode"] == "FORWARD" and
                (affected is None or affected >= now-timedelta(minutes=35))):
            raise ValueError("Incomplete crowding input; snapshot not finalized: " +
                             issue["reason"] + " " + str(issue.get("row_hash")))
    closed_rows = []
    late = []
    for row in rows:
        b = bucket(row["scan_time_jst"])
        if b > end_bucket:
            continue
        if old_watermark is not None and b <= old_watermark:
            if row["row_hash"] not in seen:
                late.append({"reason": "LATE_ARRIVING_DATA", "row_hash": row["row_hash"],
                             "source_file": row["source_file"], "bucket": iso(b),
                             "observed_at_jst": iso(now)})
                seen.add(row["row_hash"])
            continue
        if not (first_run and state["evaluation_mode"] == "FORWARD" and
                b < bucket(now)-timedelta(minutes=30)):
            closed_rows.append(row)
        seen.add(row["row_hash"])
    audit_once(state, source_audit + late)
    events, duplicates = unique_events(closed_rows, asset_map)
    audit_once(state, duplicates)
    state["snapshots"].append({"source_cutoff_jst": iso(now), "files": manifest,
                              "git_commit_hash": commit})
    snapshot_id = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    # Freeze earlier buckets before later ones. The regime function excludes current bucket.
    for event in sorted(events, key=lambda e: (e["scan_bucket_jst"], e["event_id"])):
        window_start = dt(event["scan_bucket_jst"])-timedelta(minutes=30)
        state["history"] = [h for h in state["history"] if dt(h["scan_bucket_jst"]) >= window_start]
        event.update(regime(event, state["history"]))
        state["history"].append({k: event[k] for k in (
            "event_id", "scan_bucket_jst", "symbol", "direction")})
        if state["evaluation_mode"] == "FORWARD" and (
                dt(event["scan_bucket_jst"]) < dt(state["forward_start_jst"])):
            continue  # bootstrapped context only, never count development trades as forward
        event.update(evaluation_mode=state["evaluation_mode"],
                     evaluation_phase=phase(event["scan_bucket_jst"]) if
                         state["evaluation_mode"] == "FORWARD" else "DEVELOPMENT",
                     decision_created_at_jst=iso(now), source_cutoff_jst=iso(now),
                     snapshot_id=snapshot_id, git_commit_hash=commit,
                     forward_start_jst=state["forward_start_jst"])
        state["events"].append(event)
        state["processed_event_ids"].append(event["event_id"])
        members = membership(event)
        state["memberships"].extend(members)
        execution = entry_execution(event)
        state["executions"].append(execution)
        if any(m["eligible"] for m in members):
            trade = {"event_id": event["event_id"], "trade_id": event["event_id"]+"_B1_REFERENCE",
                     "entry_price": event["entry_price"], "entry_time_jst": event["entry_time_jst"],
                     "direction": event["direction"], "risk_units": 1,
                     "sl_price": prices(event)[0], "tp_price": prices(event)[1],
                     "spec_version": event["spec_version"]}
            state["trades"].append(trade)
            state["open_shadow_positions"][event["event_id"]] = trade
    # Concurrent counts are bucket-level observations, never filters.
    by_bucket = defaultdict(list)
    core_ids = {m["event_id"] for m in state["memberships"] if m["model_id"] == CORE and m["eligible"]}
    for event in state["events"]:
        if event["event_id"] in core_ids:
            by_bucket[event["scan_bucket_jst"]].append(event)
    for event in state["events"]:
        group = by_bucket[event["scan_bucket_jst"]]
        event.update(concurrent_core_count=len(group),
                     concurrent_long_count=sum(e["direction"] == "LONG" for e in group),
                     concurrent_short_count=sum(e["direction"] == "SHORT" for e in group),
                     concurrent_crypto_count=sum(e["asset_class"] == "CRYPTO_NATIVE" for e in group))
    state["last_processed_bucket"] = iso(end_bucket)
    state["last_cutoff_jst"] = iso(now)
    state["seen_rows"] = sorted(seen)
    return settle(state, market or {}, now, settings, slices)


def settle(state, market, asof, settings=None, slices=(), report_blocks=()):
    state = copy.deepcopy(state)
    now = dt(asof)
    # Retain actual observations across restarts. Never overwrite finalized outcomes.
    market = market or {}
    for symbol, data in market.items():
        observations = state["observations"].setdefault(symbol, [])
        existing = {json.dumps(q, sort_keys=True) for q in observations}
        for q in data.get("observations", []):
            if dt(q["timestamp"]) <= now and json.dumps(q, sort_keys=True) not in existing:
                observations.append(q)
                existing.add(json.dumps(q, sort_keys=True))
    event_map = {e["event_id"]: e for e in state["events"]}
    executions = {e["event_id"]: e for e in state["executions"]}
    for eid in list(state["open_shadow_positions"]):
        event = event_map[eid]
        data = market.get(event["symbol"])
        if data is None:
            continue
        observations = state["observations"].get(event["symbol"], [])
        outcome = evaluate(event, data.get("bars", []), observations, now, data.get("fine_bars", []))
        if outcome is None:
            continue
        outcome["execution_decision_time"] = iso(now)
        # Earliest actual bid/ask at/after the reference result, available by this decision.
        # Never synthesize a quote at the candle threshold crossing.
        from .source_reader import number
        def valid_quote(q):
            bid, ask = number(q.get("bid")), number(q.get("ask"))
            return bid is not None and ask is not None and 0 < bid <= ask
        candidates = sorted((q for q in observations
                             if dt(outcome["exit_timestamp"]) <= dt(q["timestamp"]) <= now
                             and valid_quote(q)), key=lambda q: dt(q["timestamp"]))
        quote = candidates[0] if candidates else None
        outcome.update(costs(event, outcome, executions[eid], settings or {}, quote))
        outcome["cost_settings"] = dict(settings or {})
        state["results"].append(outcome)
        executions[eid].update({k: v for k, v in outcome.items()
                                if k.startswith("exit_") or k in (
                                    "gross_R", "spread_R", "fee_R", "slippage_R", "net_R",
                                    "cost_status", "cost_method", "cost_basis", "cost_settings",
                                    "missing_cost_inputs", "execution_decision_time", "spread_definition")})
        del state["open_shadow_positions"][eid]
    state["reports"] = reports(state, slices, report_blocks)
    return state

