from .config import dt, iso
from .source_reader import number


def entry_execution(event):
    bid, ask = number(event.get("bid")), number(event.get("ask"))
    valid = bid is not None and ask is not None and 0 < bid <= ask
    fill = (ask if event["direction"] == "LONG" else bid) if valid else None
    ticker = event.get("ticker_time_jst")
    age = None
    if ticker:
        try:
            age = (dt(event["decision_created_at_jst"])-dt(ticker)).total_seconds()*1000
        except ValueError:
            pass
    return {
        "event_id": event["event_id"], "signal_price": event["entry_price"],
        "expected_fill_price": fill, "bid": bid, "ask": ask,
        "spread_pct": number(event.get("spread_pct")),
        "spread_bps": (ask-bid)/event["entry_price"]*10000 if valid else None,
        "source_scan_time": event["source_scan_time"],
        "decision_time": event["decision_created_at_jst"], "ticker_time": ticker,
        "data_age_ms": age, "execution_status": "REFERENCE_QUOTE" if valid else "MISSING_QUOTE",
    }


def costs(event, result, execution, settings, exit_quote=None, method="components"):
    """Independent components on observed expected notionals, never invented costs.

    spread_R retains the established signed reference-to-quote price impact.
    For a delayed quote it includes market movement; it is NOT pure bid/ask width.
    Observation timestamps and delay are exported to make that distinction explicit.
    """
    if method not in ("components", "fills"):
        raise ValueError("Unknown cost method")
    out = {"spread_R": None, "fee_R": None, "slippage_R": None,
           "net_R": None, "cost_status": "UNRESOLVED_RESULT", "cost_method": method,
           "cost_basis": "OBSERVED_EXPECTED_NOTIONAL",
           "spread_definition": "SIGNED_REFERENCE_TO_QUOTE_IMPACT",
           "exit_bid": None, "exit_ask": None, "exit_quote_timestamp": None,
           "exit_expected_fill_price": None, "exit_data_age_ms": None,
           "exit_quote_delay_ms": None,
           "execution_decision_time": result.get("execution_decision_time", result["exit_timestamp"])}
    required = ("fee_bps_entry", "fee_bps_exit", "slippage_bps_entry", "slippage_bps_exit")
    values = {k: number(settings.get(k)) for k in required}
    if any(v is not None and v < 0 for v in values.values()):
        raise ValueError("Cost settings must be nonnegative")
    fee_ready = all(values[k] is not None for k in required[:2])
    slip_ready = all(values[k] is not None for k in required[2:])
    entry_fill = number(execution.get("expected_fill_price"))
    exit_fill = None
    sign = 1 if event["direction"] == "LONG" else -1
    if exit_quote:
        timestamp = dt(exit_quote["timestamp"])
        decision = dt(out["execution_decision_time"])
        # Never use a future observation or a pre-exit quote as the exit execution.
        if dt(result["exit_timestamp"]) <= timestamp <= decision:
            bid, ask = number(exit_quote.get("bid")), number(exit_quote.get("ask"))
            out.update(exit_bid=bid, exit_ask=ask, exit_quote_timestamp=iso(timestamp),
                       exit_data_age_ms=(decision-timestamp).total_seconds()*1000,
                       exit_quote_delay_ms=(timestamp-dt(result["exit_timestamp"])).total_seconds()*1000,
                       exit_quote_source=exit_quote.get("source"))
            if bid is not None and ask is not None and 0 < bid <= ask:
                exit_fill = bid if sign == 1 else ask
                out["exit_expected_fill_price"] = exit_fill
    gross = result.get("gross_R")
    if gross is None:
        return out
    risk = event["entry_price"]*0.005
    quotes_ready = entry_fill is not None and entry_fill > 0 and exit_fill is not None
    missing = []
    if not fee_ready:
        missing.append("MISSING_FEE_CONFIG")
    if not slip_ready:
        missing.append("MISSING_SLIPPAGE_CONFIG")
    if not quotes_ready:
        missing.append("MISSING_EXECUTION_QUOTE")
    if quotes_ready:
        reference_exit = result["exit_reference_price"]
        out["spread_R"] = sign*((entry_fill-event["entry_price"])+(reference_exit-exit_fill))/risk
        if fee_ready:
            out["fee_R"] = (entry_fill*values["fee_bps_entry"] + exit_fill*values["fee_bps_exit"])/10000/risk
        if slip_ready:
            out["slippage_R"] = (entry_fill*values["slippage_bps_entry"] + exit_fill*values["slippage_bps_exit"])/10000/risk
    out["missing_cost_inputs"] = missing
    out["cost_status"] = missing[0] if missing else "COMPLETE"
    if not missing:
        base = gross-out["spread_R"] if method == "components" else sign*(exit_fill-entry_fill)/risk
        out["net_R"] = base-out["fee_R"]-out["slippage_R"]
    return out
