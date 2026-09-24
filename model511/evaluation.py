"""Descriptive forward reports; no fitting or parameter search."""
from collections import defaultdict
import random
from statistics import mean
from .config import dt
from .model_league import CORE


def cluster_ci(rows, key, seed=511, samples=2000):
    groups = defaultdict(list)
    for row in rows:
        if row.get("net_R") is None:
            return None
        groups[row[key]].append(row["net_R"])
    groups = list(groups.values())
    if len(groups) < 2:
        return None
    rng = random.Random(seed)
    estimates = []
    for _ in range(samples):
        selected = [groups[rng.randrange(len(groups))] for _ in groups]
        estimates.append(sum(sum(g) for g in selected)/sum(len(g) for g in selected))
    estimates.sort()
    return [estimates[int(samples*0.025)], estimates[min(samples-1, int(samples*0.975))]]


def summarize(rows):
    resolved = [r for r in rows if r.get("gross_R") is not None]
    n = len(resolved)
    net_complete = n > 0 and all(r.get("net_R") is not None for r in resolved)
    net_ev = mean(r["net_R"] for r in resolved) if net_complete else None
    clusters = {r["cluster_30m_id"] for r in resolved}
    ci = cluster_ci(resolved, "cluster_30m_id") if net_complete else None
    qualification = "INSUFFICIENT_DATA" if n < 100 or len(clusters) < 30 else (
        "NEGATIVE" if net_ev is not None and net_ev < 0 else
        "FORWARD_SUPPORTED" if net_ev is not None and net_ev > 0 and ci and ci[0] > 0 else
        "INCONCLUSIVE")
    equity = peak = dd = 0.0
    streak = max_streak = 0
    for row in sorted(resolved, key=lambda r: (r["exit_timestamp"], r["event_id"])):
        equity += row["gross_R"]
        peak = max(peak, equity)
        dd = max(dd, peak-equity)
        streak = streak+1 if row["gross_R"] < 0 else 0
        max_streak = max(max_streak, streak)
    net_n = sum(r.get("net_R") is not None for r in resolved)
    out = {"N": len(rows), "resolved_N": n, "resolved_gross_n": n,
           "net_evaluable_n": net_n, "net_coverage_ratio": net_n/n if n else None,
           "net_coverage_complete": net_complete,
           "WIN": sum(r["exit_result"] == "TP" for r in rows),
           "LOSS": sum(r["exit_result"] == "SL" for r in rows),
           "TIME": sum(r["exit_result"] == "TIME" for r in rows),
           "UNKNOWN": sum(r["exit_result"] == "UNKNOWN" for r in rows),
           "gross_R_total": sum(r["gross_R"] for r in resolved),
           "gross_EV": mean(r["gross_R"] for r in resolved) if n else None,
           "net_R_total": sum(r["net_R"] for r in resolved) if net_complete else None,
           "net_EV": net_ev, "net_complete_N": sum(r.get("net_R") is not None for r in resolved),
           "win_rate": sum(r["gross_R"] > 0 for r in resolved)/n if n else None,
           "max_drawdown_R": dd, "max_loss_streak": max_streak,
           "30m_clusters": len(clusters), "cluster_bootstrap_95_CI": ci,
           "day_bootstrap_95_CI": cluster_ci(resolved, "date") if net_complete else None,
           "qualification": qualification}
    for flag in ("ambiguous_intrabar", "ambiguous_deadline_boundary"):
        out[flag + "_N"] = sum(bool(r.get(flag)) for r in rows)
        out[flag + "_ratio"] = out[flag + "_N"]/len(rows) if rows else None
    out["UNKNOWN_ratio"] = out["UNKNOWN"]/len(rows) if rows else None
    for window in (5, 30):
        grouped = defaultdict(list)
        for r in resolved:
            grouped[r[f"cluster_{window}m_id"]].append(r["gross_R"])
        out[f"{window}m_cluster_EV"] = mean(mean(g) for g in grouped.values()) if grouped else None
    return out


def validate_blocks(blocks):
    names = set()
    for block in blocks:
        if not block.get("name") or block["name"] in names:
            raise ValueError("Report block names must be unique and nonempty")
        if dt(block["start"]) >= dt(block["end"]):
            raise ValueError("Report block requires start < end")
        names.add(block["name"])
    return blocks


def reports(state, slices=(), blocks=()):
    validate_blocks(blocks)
    events = {e["event_id"]: e for e in state["events"]}
    outcomes = {r["event_id"]: r for r in state["results"]}
    grouped = defaultdict(list)
    for member in state["memberships"]:
        eid = member["event_id"]
        event = events[eid]
        key = (member["model_id"], event["evaluation_mode"], event["evaluation_phase"])
        grouped[key]
        if not member["eligible"] or eid not in outcomes:
            continue
        row = {**event, **outcomes[eid], "date": dt(event["entry_time_jst"]).date().isoformat()}
        key = (member["model_id"], event["evaluation_mode"], event["evaluation_phase"])
        grouped[key].append(row)
    output = []
    for (model, mode, phase), rows in sorted(grouped.items()):
        base = {"model_id": model, "evaluation_mode": mode, "evaluation_phase": phase}
        summary = summarize(rows)
        if mode != "FORWARD" or phase != "MAIN_FORWARD" or model != CORE:
            summary["qualification"] = "NOT_PRIMARY_FORWARD_CORE"
        output.append({**base, "slice": "ALL", **summary})
        for block in blocks:
            subset = [r for r in rows if dt(block["start"]) <= dt(r["entry_time_jst"]) < dt(block["end"])]
            metrics = summarize(subset)
            metrics["qualification"] = "DESCRIPTIVE_BLOCK_ONLY"
            output.append({**base, "slice": "WALK_FORWARD_BLOCK", "slice_value": block["name"],
                           "block_start": block["start"], "block_end": block["end"], **metrics})
        for field in slices:
            pieces = defaultdict(list)
            for row in rows:
                pieces[str(row.get(field, "MISSING"))].append(row)
            for value, subset in sorted(pieces.items()):
                metrics = summarize(subset)
                metrics["qualification"] = "DESCRIPTIVE_SLICE_ONLY"
                output.append({**base, "slice": field, "slice_value": value, **metrics})
    return output



def main():
    """Read-only reporting from a saved state; never changes membership or research rules."""
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description="511 descriptive reports only")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--blocks", type=Path)
    parser.add_argument("--slices", nargs="*", default=[])
    args = parser.parse_args()
    state = json.loads(args.state.read_text("utf-8"))
    blocks = json.loads(args.blocks.read_text("utf-8")) if args.blocks else []
    print(json.dumps(reports(state, args.slices, blocks), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
