from datetime import timedelta
from .config import bucket, dt


def regime(event, history):
    end = bucket(event["scan_bucket_jst"])
    start = end - timedelta(minutes=30)
    counts = {start + timedelta(minutes=5*i): {"LONG": 0, "SHORT": 0} for i in range(6)}
    seen = set()
    for prior in history:
        when = dt(prior["scan_bucket_jst"])
        if start <= when < end and prior["event_id"] not in seen:
            counts[when][prior["direction"]] += 1
            seen.add(prior["event_id"])
    long = sum(c["LONG"] for c in counts.values())
    short = sum(c["SHORT"] for c in counts.values())
    majority = "LONG" if long > short else "SHORT" if short > long else "NEUTRAL"
    directions = ["LONG" if c["LONG"] > c["SHORT"] else
                  "SHORT" if c["SHORT"] > c["LONG"] else "NEUTRAL" for c in counts.values()]
    # Literal specification: count buckets matching the 30m majority, including NEUTRAL.
    persistence = sum(d == majority for d in directions)
    total = long + short
    return {
        "prior_long_count": long, "prior_short_count": short, "prior_total_count": total,
        "majority_direction": majority, "majority_count": max(long, short),
        "minority_count": min(long, short),
        "majority_ratio": max(long, short) / total if total else 0.0,
        "direction_imbalance": (long-short) / total if total else 0.0,
        "crowding_state": "NEUTRAL" if majority == "NEUTRAL" else
                          "MATCH" if majority == event["direction"] else "OPPOSITE",
        "persistence_score": persistence,
        "persistence_category": "LOW" if persistence <= 2 else "MID" if persistence <= 4 else "HIGH",
    }

