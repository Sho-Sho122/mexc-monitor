from collections import defaultdict
from .config import SPEC_VERSION, SPEC_HASH, bucket, dt, iso


def unique_events(rows, asset_map=None):
    groups = defaultdict(list)
    asset_map = asset_map or {}
    for row in rows:
        key = (row["symbol"], row["direction"], iso(bucket(row["scan_time_jst"])))
        groups[key].append(row)
    events, audit = [], []
    for (symbol, direction, b), signals in sorted(groups.items()):
        signals.sort(key=lambda r: (dt(r["scan_time_jst"]), r["strategy"], r["row_hash"]))
        pb = [r for r in signals if r["strategy"] == "PULLBACK"]
        ref = (pb or signals)[0]
        names = sorted({r["strategy"] for r in signals})
        # Preserve source symbol exactly to avoid normalization collisions.
        event_id = f"511_{dt(b):%Y%m%d%H%M}_{symbol}_{direction}"
        if len(pb) > 1:
            audit.append({"event_id": event_id, "reason": "DUPLICATE_PULLBACK",
                          "row_hashes": [r["row_hash"] for r in pb]})
        context = {k: v for k, v in ref.items() if k.startswith("btc_") or k in {
            "funding_rate", "oi", "oi_change_pct", "volume_ratio1", "volume_ratio5",
            "volume_ratio15", "atr1_pct", "atr5_pct", "atr15_pct", "bid", "ask",
            "spread_pct", "price", "ticker_time_jst"}}
        event = {
            **context, "event_id": event_id, "scan_bucket_jst": b,
            "symbol": symbol, "direction": direction,
            "strategy_set": "|".join(names), "strategy_count": len(names),
            "entry_price": ref["entry"], "signal_price": ref["entry"],
            "entry_time_jst": ref["scan_time_jst"],
            "source_scan_time": ref["scan_time_jst"],
            "reference_strategy": ref["strategy"], "reference_row_hash": ref["row_hash"],
            "source_row_hashes": sorted({r["row_hash"] for r in signals}),
            "asset_class": asset_map.get(symbol, "UNKNOWN"),
            "spec_version": SPEC_VERSION, "spec_hash": SPEC_HASH,
            "cluster_5m_id": f"{dt(b):%Y%m%d%H%M}_{direction}",
            "cluster_30m_id": f"{bucket(b, 30):%Y%m%d%H%M}_{direction}",
        }
        for name in ("PULLBACK", "TREND_VOLUME", "OI_FUNDING", "HIGH_EDGE"):
            event["has_" + name.lower()] = name in names
        events.append(event)
    return events, audit

