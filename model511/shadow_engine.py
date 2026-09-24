"""Reference outcomes only. Quotes for TIME are separate from candle closes."""
import math
from datetime import timedelta
from .config import dt, iso, bucket


def hit(bar, entry, direction):
    sl = entry * (0.995 if direction == "LONG" else 1.005)
    tp = entry * (1.005 if direction == "LONG" else 0.995)
    if direction == "LONG":
        return bar["high"] >= tp, bar["low"] <= sl
    return bar["low"] <= tp, bar["high"] >= sl


def prices(event):
    entry = event["entry_price"]
    return (entry * (0.995 if event["direction"] == "LONG" else 1.005),
            entry * (1.005 if event["direction"] == "LONG" else 0.995))


def evaluate(event, bars, observations, asof, fine_bars=()):
    """None means required data not yet available; never substitute an earlier close.

    Full bars begin at the first 5m boundary at/after entry, matching research_exit.
    A boundary candle can prove absence of touches, never their pre-deadline timing.
    Fine bars must supply continuous coverage of the pre-deadline interval.
    """
    start, asof = dt(event["entry_time_jst"]), dt(asof)
    deadline = start + timedelta(hours=3)
    entry, direction = event["entry_price"], event["direction"]
    sl, tp = prices(event)
    first = bucket(start)
    if first < start:
        first += timedelta(minutes=5)
    quotes = sorted((q for q in observations
                     if deadline <= dt(q["timestamp"]) <= asof and
                     q.get("price") is not None and q["price"] > 0),
                    key=lambda q: (dt(q["timestamp"]), q["source"]))
    time_quote = quotes[0] if quotes else None

    def result(kind, when, price=None, reason=None, boundary=False):
        gross = None if price is None else (
            (price-entry) if direction == "LONG" else (entry-price)) / (entry*0.005)
        return {
            "event_id": event["event_id"], "trade_id": event["event_id"] + "_B1_REFERENCE",
            "exit_result": kind, "exit_reference_price": price,
            "exit_timestamp": iso(when), "gross_R": gross,
            "ambiguous_intrabar": int(kind == "UNKNOWN" and not boundary),
            "ambiguous_deadline_boundary": int(boundary), "unknown_reason": reason,
            "deadline_jst": iso(deadline),
            "exit_price_source": time_quote["source"] if kind == "TIME" else "KLINE",
            "time_exit_audit_price": time_quote["price"] if time_quote else None,
            "time_exit_audit_timestamp": time_quote["timestamp"] if time_quote else None,
            "time_exit_audit_source": time_quote["source"] if time_quote else None,
        }

    def check(bar):
        tp_hit, sl_hit = hit(bar, entry, direction)
        if tp_hit and sl_hit:
            return result("UNKNOWN", bar["end"], reason="INTRABAR_TP_SL")
        if tp_hit:
            return result("TP", bar["end"], tp)
        if sl_hit:
            return result("SL", bar["end"], sl)
        return None

    # Never consume unclosed candles or bars prior to the first eligible full bar.
    usable = sorted((b for b in bars if dt(b["start"]) >= first and dt(b["end"]) <= asof),
                    key=lambda b: dt(b["start"]))
    cursor = first
    for bar in usable:
        left, right = dt(bar["start"]), dt(bar["end"])
        if left >= deadline:
            break
        if left < cursor:
            continue
        if left != cursor:
            return None  # no synthetic gap filling
        if right <= deadline:
            out = check(bar)
            if out:
                return out
            cursor = right
            continue
        # This bar straddles the deadline.
        if not any(hit(bar, entry, direction)):
            cursor = deadline  # full range proves no pre-deadline touch
            break
        fine = sorted((b for b in fine_bars if left <= dt(b["start"]) < deadline
                       and dt(b["end"]) <= asof), key=lambda b: dt(b["start"]))
        covered = left
        for small in fine:
            a, z = dt(small["start"]), dt(small["end"])
            if a < covered:
                continue
            if a != covered:
                break
            if z <= deadline:
                out = check(small)
                if out:
                    return out
                covered = z
            elif not any(hit(small, entry, direction)):
                covered = deadline
            else:
                break
            if covered >= deadline:
                break
        if covered < deadline:
            return result("UNKNOWN", deadline, reason=
                          "DEADLINE_BOUNDARY_INSUFFICIENT_GRANULARITY", boundary=True)
        cursor = deadline
        break
    if cursor < deadline or asof < deadline or time_quote is None:
        return None
    return result("TIME", time_quote["timestamp"], time_quote["price"])

