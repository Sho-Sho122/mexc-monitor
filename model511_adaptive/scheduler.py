"""Fixed anchor calendar; no performance-dependent scheduling."""
from datetime import timedelta, time
from .config import dt, iso


def due(anchor, cutoff, days, completed=()):
    anchor, cutoff = dt(anchor), dt(cutoff)
    known = set(completed)
    t = anchor + timedelta(days=days)
    result = []
    while t <= cutoff:
        if iso(t) not in known:
            result.append(iso(t))
        t += timedelta(days=days)
    return result


def daily_cutoff(start, clock, at):
    parsed = time.fromisoformat(clock)
    if parsed.tzinfo or parsed.microsecond or len(clock) != 8:
        raise ValueError('Daily JST clock must be HH:MM:SS')
    at = dt(at)
    scheduled = at.replace(hour=parsed.hour, minute=parsed.minute, second=parsed.second, microsecond=0)
    if scheduled > at:
        scheduled -= timedelta(days=1)
    return scheduled if scheduled > dt(start) else None
