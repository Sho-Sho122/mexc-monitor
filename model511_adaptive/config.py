"""Frozen policy. Execution settings are supplied separately, never inferred."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = '1.1 FINAL'
SCHEMA = 2
JST = timezone(timedelta(hours=9))
SPEC_PATH = Path(__file__).resolve().parents[1] / 'docs/511_ADAPTIVE_RESEARCH_MODEL_LEAGUE_SPEC_v1.1_FINAL_RECONCILED.md'
BOOTSTRAP = {'seed': 511, 'samples': 2000, 'unit': '30m_time', 'ci': .95,
             'method': 'percentile; event-weighted cluster resampling'}
GATE = {'days': 10, 'n': 150, 'clusters': 60, 'active_days': 7}
PRACTICE = {'days': 28, 'n': 400, 'clusters': 150, 'active_days': 20}
FINAL = {'days': 14, 'n': 150, 'clusters': 60, 'active_days': 10}
COST_KEYS = ('fee_bps_entry', 'fee_bps_exit', 'slippage_bps_entry', 'slippage_bps_exit')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def spec_hash():
    return hashlib.sha256(SPEC_PATH.read_bytes()).hexdigest()


def dt(value):
    out = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if out.tzinfo is None:
        raise ValueError('Adaptive timestamps must have an explicit timezone')
    return out.astimezone(JST)


def iso(value):
    return dt(value).isoformat()


def now():
    return datetime.now(JST)


def bucket(value, minutes=5):
    t = dt(value)
    return t.replace(minute=t.minute // minutes * minutes, second=0, microsecond=0)


def next_bucket(value):
    return bucket(value) + timedelta(minutes=5)


def base_model():
    return {'entry': [{'field': 'has_pullback', 'op': 'eq', 'value': True},
                      {'field': 'crowding_state', 'op': 'eq', 'value': 'OPPOSITE'}],
            'exit': {'sl_pct': .50, 'rr': 1., 'max_hours': 3},
            'feature_schema_version': '1', 'universe': 'ALL_OBSERVED',
            'reference_policy': 'BETA1_BOUNDARIES', 'cost_policy': 'OBSERVED_EXPECTED_NOTIONAL'}
