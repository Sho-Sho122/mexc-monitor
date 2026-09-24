"""Read all four completed strategy outputs as one auditable snapshot."""
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from .config import STRATEGIES, dt, iso


def number(value):
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (ValueError, TypeError):
        return None


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_snapshot(root, cutoff, verify_states=False, not_before=None):
    root, cutoff = Path(root), dt(cutoff)
    payloads, manifest, audit = {}, [], []
    scans = set()
    for strategy in STRATEGIES:
        path = root / ("strategy_" + strategy.lower() + ".csv")
        raw = path.read_bytes()  # missing source is a failed snapshot, not zero signals
        payloads[strategy] = (path, raw)
        if verify_states:
            state = json.loads(path.with_name(path.stem + "_state.json").read_text("utf-8"))
            if not state.get("last_scan"):
                raise ValueError("Source state has no last_scan: " + strategy)
            scans.add(state["last_scan"])
    if verify_states:
        if len(scans) != 1:
            raise ValueError("Strategies did not finish the same scan")
        scan = root / next(iter(scans))
        if not scan.is_file():
            raise ValueError("Current scanner output is missing")
        if not_before is not None and scan.stat().st_mtime < dt(not_before).timestamp():
            raise ValueError("Scanner output predates this workflow run")
    valid = []
    for strategy, (path, raw) in payloads.items():
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        required = {"event", "symbol", "direction", "entry", "scan_time_jst"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Missing required columns: " + path.name)
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("Duplicate columns: " + path.name)
        count, last = 0, None
        for line, row in enumerate(reader, 2):
            count += 1
            fingerprint = digest(json.dumps(list(row.items()), ensure_ascii=False).encode())
            source = {"source_file": path.name, "source_line": line, "row_hash": fingerprint,
                      "source_time": row.get("time"), "scan_time_jst": row.get("scan_time_jst")}
            if None in row or None in row.values():
                audit.append({**source, "reason": "MALFORMED_CSV_ROW"})
                continue
            if row["event"] != "OPEN":
                continue
            try:
                when = dt(row["scan_time_jst"])
            except (ValueError, TypeError):
                audit.append({**source, "reason": "INVALID_SCAN_TIME"})
                continue
            last = when if last is None else max(last, when)
            if when > cutoff:
                audit.append({**source, "reason": "FUTURE_SCAN_TIME"})
                continue
            if not row["symbol"].strip() or row["direction"] not in ("LONG", "SHORT"):
                audit.append({**source, "reason": "INVALID_SYMBOL_OR_DIRECTION"})
                continue
            entry = number(row["entry"])
            if entry is None or entry <= 0:
                audit.append({**source, "reason": "INVALID_ENTRY"})
                continue
            valid.append({**row, **source, "strategy": strategy,
                          "scan_time_jst": iso(when), "entry": entry})
        manifest.append({"source_file": path.name, "source_hash": digest(raw),
                         "source_row_count": count,
                         "source_last_timestamp": iso(last) if last else None})
    for path, raw in payloads.values():
        if digest(path.read_bytes()) != digest(raw):
            raise ValueError("Source changed during snapshot: " + path.name)
    return valid, manifest, audit

