"""One atomic JSON commit is authoritative; CSVs are rebuildable projections."""
import csv
import hashlib
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
from .config import SPEC_HASH, SPEC_VERSION

TABLES = {
    "events": "511_unique_events.csv", "memberships": "511_model_membership.csv",
    "trades": "511_shadow_trades.csv", "results": "511_shadow_results.csv",
    "executions": "511_execution.csv", "reports": "511_report.csv",
}


def atomic(path, raw):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


@contextmanager
def lock(directory):
    """OS advisory lock is released by the kernel even after a killed process."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".writer.lock"
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise FileExistsError("Another 511 writer holds the lock") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def empty(mode, commit):
    return {
        "schema_version": 1, "spec_version": SPEC_VERSION, "spec_hash": SPEC_HASH,
        "evaluation_mode": mode, "git_commit_hash": commit, "forward_start_jst": None,
        "last_processed_bucket": None, "last_cutoff_jst": None,
        "processed_event_ids": [], "open_shadow_positions": {},
        "history": [], "seen_rows": [], "audit": [], "snapshots": [],
        "observations": {}, **{k: [] for k in TABLES},
    }


def read(directory, mode, commit):
    path = Path(directory) / "511_state.json"
    if not path.exists():
        return empty(mode, commit)
    state = json.loads(path.read_text("utf-8"))  # corruption must never reset the league
    if state["spec_hash"] != SPEC_HASH or state["evaluation_mode"] != mode:
        raise ValueError("State spec/mode mismatch")
    if state.get("schema_version") != 1:
        raise ValueError("Unsupported state schema")
    return state


def write(directory, state):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    atomic(directory / "511_state.json", raw)
    export(directory, state)


def export(directory, state):
    directory = Path(directory)
    for key, name in TABLES.items():
        rows = state[key]
        fields = sorted({field for row in rows for field in row}) or ["event_id"]
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False, sort_keys=True)
                             if isinstance(v, (list, dict)) else v for k, v in row.items()})
        atomic(directory / name, buffer.getvalue().encode("utf-8-sig"))
    raw = "\n".join(json.dumps(a, ensure_ascii=False, sort_keys=True) for a in state["audit"])
    atomic(directory / "511_audit.log", (raw + "\n").encode())

