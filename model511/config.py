"""Research constants are not runtime tuning options."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
SPEC_VERSION = "1.0"
STRATEGIES = ("HIGH_EDGE", "OI_FUNDING", "PULLBACK", "TREND_VOLUME")
RESEARCH = {
    "spec_version": SPEC_VERSION, "bucket_minutes": 5, "crowding_minutes": 30,
    "core": "has_pullback AND crowding_state == OPPOSITE",
    "sl_fraction": 0.005, "rr": 1.0, "max_hours": 3,
    "persistence": {"LOW": [0, 2], "MID": [3, 4], "HIGH": [5, 6]},
    "reference_entry": "earliest PULLBACK else earliest signal; tie=strategy,row_hash",
    "finalization": "first successful snapshot after bucket end; immutable",
    "deadline_boundary": "fine data or proven no touch; otherwise UNKNOWN",
    "time_exit": "first observed price at/after deadline, never earlier bar close",
}
SPEC_HASH = hashlib.sha256(json.dumps(RESEARCH, sort_keys=True).encode()).hexdigest()
MAIN_START = datetime(2026, 10, 1, tzinfo=JST)
MAIN_END = datetime(2026, 10, 15, tzinfo=JST)


def dt(value):
    if isinstance(value, datetime):
        out = value
    else:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return out.replace(tzinfo=JST) if out.tzinfo is None else out.astimezone(JST)


def iso(value):
    return dt(value).isoformat()


def bucket(value, minutes=5):
    value = dt(value)
    return value.replace(minute=value.minute // minutes * minutes, second=0, microsecond=0)


def phase(value):
    value = dt(value)
    if value < MAIN_START:
        return "SHAKEDOWN"
    return "MAIN_FORWARD" if value < MAIN_END else "POST_FORWARD"

