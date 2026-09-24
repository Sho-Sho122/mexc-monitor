import argparse
import json
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from .config import JST, dt
from .source_reader import read_snapshot
from .storage import lock, read, write, export
from .runner import process, settle, audit_once


def main():
    parser = argparse.ArgumentParser(description="511 beta1 SHADOW ONLY")
    parser.add_argument("--mode", choices=("FORWARD", "BACKFILL"), required=True)
    parser.add_argument("--source-dir", type=Path, default=Path("."))
    parser.add_argument("--output-dir", type=Path, default=Path("511_data"))
    parser.add_argument("--as-of", help="Required recorded cutoff for BACKFILL; forbidden in FORWARD")
    parser.add_argument("--market-file", type=Path, help="Recorded bars and observations JSON")
    parser.add_argument("--cost-config", type=Path)
    parser.add_argument("--asset-map", type=Path)
    parser.add_argument("--verify-strategy-states", action="store_true")
    parser.add_argument("--run-start", help="Workflow start timestamp for scanner freshness")
    parser.add_argument("--fetch-market", action="store_true")
    parser.add_argument("--slices", nargs="*", default=[])
    parser.add_argument("--report-blocks", type=Path, help="Descriptive JSON blocks: name/start/end")
    args = parser.parse_args()
    if args.mode == "FORWARD" and args.as_of:
        parser.error("FORWARD cannot use an artificial clock")
    if args.mode == "BACKFILL" and (not args.as_of or args.fetch_market):
        parser.error("BACKFILL requires --as-of and recorded market data, never live retrieval")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.source_dir, text=True).strip()
    cutoff = dt(args.as_of) if args.as_of else datetime.now(JST)
    settings = json.loads(args.cost_config.read_text("utf-8")) if args.cost_config else {}
    from .evaluation import validate_blocks
    blocks = validate_blocks(json.loads(args.report_blocks.read_text("utf-8"))) if args.report_blocks else []
    asset_map = {}
    if args.asset_map:
        mapping = json.loads(args.asset_map.read_text("utf-8"))
        if not mapping.get("version") or not mapping.get("source"):
            raise ValueError("Asset mapping requires version and source")
        asset_map = mapping["symbols"]
        allowed = {"CRYPTO_NATIVE", "COMMODITY_LINKED", "EQUITY_ETF_LINKED", "INDEX_LINKED", "UNKNOWN"}
        if any(v not in allowed for v in asset_map.values()):
            raise ValueError("Invalid asset class")
    outdir = args.output_dir / args.mode.lower()
    with lock(outdir):
        state = read(outdir, args.mode, commit)
        rows, manifest, audit = read_snapshot(args.source_dir, cutoff,
                                              args.verify_strategy_states, args.run_start)
        try:
            state = process(state, rows, manifest, audit, cutoff, commit, asset_map=asset_map)
        except ValueError:
            audit_once(state, audit)
            write(outdir, state)
            raise
        if args.asset_map:
            state["asset_mapping"] = mapping
        # Commit decisions BEFORE network retrieval; a restart must not re-decide with later rows.
        write(outdir, state)
        market = json.loads(args.market_file.read_text("utf-8")) if args.market_file else {}
        if args.fetch_market:
            from .market_data import PublicMarketData
            client = PublicMarketData()
            grouped = defaultdict(list)
            events = {e["event_id"]: e for e in state["events"]}
            for eid in state["open_shadow_positions"]:
                grouped[events[eid]["symbol"]].append(dt(events[eid]["entry_time_jst"]))
            failures = []
            for symbol, starts in grouped.items():
                try:
                    market[symbol] = client.fetch(symbol, min(starts), datetime.now(JST),
                                                  max(starts)+timedelta(hours=3, minutes=5))
                except Exception as exc:
                    failures.append({"reason": "MARKET_DATA_ERROR", "symbol": symbol,
                                     "at": datetime.now(JST).isoformat(), "error": str(exc)})
            audit_once(state, failures)
        asof = cutoff if args.mode == "BACKFILL" else datetime.now(JST)
        state = settle(state, market, asof, settings, args.slices, blocks)
        write(outdir, state)
    print(json.dumps({"mode": args.mode, "events": len(state["events"]),
                      "open": len(state["open_shadow_positions"]), "results": len(state["results"]),
                      "forward_start_jst": state["forward_start_jst"], "output": str(outdir)}))


if __name__ == "__main__":
    main()

