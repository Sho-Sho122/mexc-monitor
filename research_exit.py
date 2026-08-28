import csv
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

BASE_URL = "https://api.mexc.com"
JST = timezone(timedelta(hours=9))

STATE_FILE = "research_exit_state.json"
TRADES_FILE = "research_exit_trades.csv"
RESULTS_FILE = "research_exit_results.csv"

API_SLEEP = 0.12
PROCESS_GRACE_MINUTES = 10

SL_PCTS = [0.25, 0.35, 0.50, 0.75, 1.00]
RRS = [1.0, 1.5, 2.0]
MAX_HOURS_LIST = [3, 6, 12, 24]

SOURCE_FILES = {
    "HIGH_EDGE": "strategy_high_edge.csv",
    "TREND_VOLUME": "strategy_trend_volume.csv",
    "PULLBACK": "strategy_pullback.csv",
    "OI_FUNDING": "strategy_oi_funding.csv",
}

SNAPSHOT_FIELDS = [
    "scan_time_jst", "ticker_time_jst",
    "price", "bid", "ask", "spread_pct",
    "volume24", "amount24", "change24",
    "funding_rate", "oi", "oi_change_pct",
    "rsi1", "rsi5", "rsi15",
    "ema9_1", "ema21_1", "ema9_5", "ema21_5", "ema9_15", "ema21_15",
    "macd1", "macd_signal1", "macd_hist1",
    "macd5", "macd_signal5", "macd_hist5",
    "macd15", "macd_signal15", "macd_hist15",
    "volume_ratio1", "volume_ratio5", "volume_ratio15",
    "atr1_pct", "atr5_pct", "atr15_pct",
    "long_score", "short_score", "bias", "edge",
    "distance_ema9_5_pct",
    "btc_price",
    "btc_ret_1m_pct", "btc_ret_5m_pct", "btc_ret_15m_pct", "btc_ret_1h_pct",
    "btc_rsi1", "btc_rsi5", "btc_rsi15", "btc_rsi60",
    "btc_ema9_1", "btc_ema21_1", "btc_ema9_5", "btc_ema21_5",
    "btc_ema9_15", "btc_ema21_15", "btc_ema9_60", "btc_ema21_60",
    "btc_macd1", "btc_macd_signal1", "btc_macd5", "btc_macd_signal5",
    "btc_macd15", "btc_macd_signal15", "btc_macd60", "btc_macd_signal60",
    "btc_volume_ratio1", "btc_volume_ratio5", "btc_volume_ratio15", "btc_volume_ratio60",
    "btc_atr1_pct", "btc_atr5_pct", "btc_atr15_pct", "btc_atr60_pct",
]

TRADE_FIELDS = [
    "trade_id", "strategy", "source_time", "symbol", "direction", "entry",
    "source_sl", "source_tp", "registered_at_jst",
    *SNAPSHOT_FIELDS,
    "snapshot_json",
]

RESULT_FIELDS = [
    "trade_id", "strategy", "symbol", "direction", "entry",
    "sl_pct", "rr", "max_hours", "sl_price", "tp_price",
    "event", "exit_price", "result_r", "holding_hours",
    "mfe_pct", "mae_pct", "exit_bar_time_jst", "note",
]


def num(x, default=0.0):
    try:
        if x in ("", None, "None", "nan", "NaN"):
            return default
        return float(x)
    except Exception:
        return default


def get_json(url, params=None):
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"MEXC API ERROR: {data}")
    return data


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"initialized": False, "source_offsets": {}, "pending": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.setdefault("initialized", False)
    state.setdefault("source_offsets", {})
    state.setdefault("pending", {})
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def append_csv(path, fields, row):
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fields})


def read_csv_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def parse_source_time(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    else:
        dt = dt.astimezone(JST)
    return dt


def make_trade_id(strategy, row):
    raw = "|".join([
        strategy,
        str(row.get("time", "")),
        str(row.get("symbol", "")),
        str(row.get("direction", "")),
        str(row.get("entry", "")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def first_full_5m_bar_ts(open_ts):
    return ((int(open_ts) + 299) // 300) * 300


def ts_to_jst_str(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")


def fetch_5m_chunk(symbol, start_ts, end_ts):
    url = f"{BASE_URL}/api/v1/contract/kline/{symbol}"
    data = get_json(url, {
        "interval": "Min5",
        "start": int(start_ts),
        "end": int(end_ts),
    })
    d = data.get("data", {})
    times = d.get("time", [])
    opens = d.get("open", [])
    closes = d.get("close", [])
    highs = d.get("high", [])
    lows = d.get("low", [])
    n = min(len(times), len(opens), len(closes), len(highs), len(lows))
    bars = []
    for i in range(n):
        bars.append({
            "time": int(times[i]),
            "open": num(opens[i]),
            "close": num(closes[i]),
            "high": num(highs[i]),
            "low": num(lows[i]),
        })
    return bars


def fetch_24h_5m_history(symbol, open_dt):
    open_ts = int(open_dt.timestamp())
    start_ts = first_full_5m_bar_ts(open_ts)
    end_ts = open_ts + 24 * 3600
    all_bars = {}
    chunk_seconds = 6 * 3600
    cursor = start_ts
    while cursor <= end_ts:
        chunk_end = min(cursor + chunk_seconds, end_ts + 300)
        bars = fetch_5m_chunk(symbol, cursor, chunk_end)
        for bar in bars:
            t = bar["time"]
            if start_ts <= t <= end_ts:
                all_bars[t] = bar
        cursor += chunk_seconds
        time.sleep(API_SLEEP)
    return [all_bars[t] for t in sorted(all_bars.keys())]


def calc_mfe_mae(bars, entry, direction):
    if not bars or entry <= 0:
        return 0.0, 0.0
    max_high = max(b["high"] for b in bars)
    min_low = min(b["low"] for b in bars)
    if direction == "LONG":
        mfe = max(0.0, (max_high - entry) / entry * 100)
        mae = max(0.0, (entry - min_low) / entry * 100)
    else:
        mfe = max(0.0, (entry - min_low) / entry * 100)
        mae = max(0.0, (max_high - entry) / entry * 100)
    return mfe, mae


def make_exit_prices(entry, direction, sl_pct, rr):
    f = sl_pct / 100.0
    if direction == "LONG":
        return entry * (1 - f), entry * (1 + f * rr)
    return entry * (1 + f), entry * (1 - f * rr)


def directional_move_pct(entry, exit_price, direction):
    if entry <= 0:
        return 0.0
    if direction == "LONG":
        return (exit_price - entry) / entry * 100
    return (entry - exit_price) / entry * 100


def evaluate_variant(trade, bars, sl_pct, rr, max_hours):
    entry = num(trade["entry"])
    direction = trade["direction"]
    open_dt = parse_source_time(trade["source_time"])
    open_ts = open_dt.timestamp()
    cutoff_ts = open_ts + max_hours * 3600
    usable = [b for b in bars if b["time"] <= cutoff_ts]
    sl_price, tp_price = make_exit_prices(entry, direction, sl_pct, rr)
    mfe_pct, mae_pct = calc_mfe_mae(usable, entry, direction)

    for bar in usable:
        if direction == "LONG":
            hit_tp = bar["high"] >= tp_price
            hit_sl = bar["low"] <= sl_price
        else:
            hit_tp = bar["low"] <= tp_price
            hit_sl = bar["high"] >= sl_price

        bar_end = bar["time"] + 300
        hold_h = round((bar_end - open_ts) / 3600, 4)
        bar_time = ts_to_jst_str(bar_end)

        if hit_tp and hit_sl:
            return {
                "sl_price": sl_price, "tp_price": tp_price,
                "event": "UNKNOWN", "exit_price": "", "result_r": "",
                "holding_hours": hold_h, "mfe_pct": mfe_pct, "mae_pct": mae_pct,
                "exit_bar_time_jst": bar_time,
                "note": "同一5分足でTP/SL双方到達・順序不明",
            }
        if hit_tp:
            return {
                "sl_price": sl_price, "tp_price": tp_price,
                "event": "TP", "exit_price": tp_price, "result_r": rr,
                "holding_hours": hold_h, "mfe_pct": mfe_pct, "mae_pct": mae_pct,
                "exit_bar_time_jst": bar_time, "note": "TP",
            }
        if hit_sl:
            return {
                "sl_price": sl_price, "tp_price": tp_price,
                "event": "SL", "exit_price": sl_price, "result_r": -1.0,
                "holding_hours": hold_h, "mfe_pct": mfe_pct, "mae_pct": mae_pct,
                "exit_bar_time_jst": bar_time, "note": "SL",
            }

    if not usable:
        return {
            "sl_price": sl_price, "tp_price": tp_price,
            "event": "NO_DATA", "exit_price": "", "result_r": "",
            "holding_hours": "", "mfe_pct": mfe_pct, "mae_pct": mae_pct,
            "exit_bar_time_jst": "", "note": "時間決済用5分足なし",
        }

    last_bar = usable[-1]
    exit_price = last_bar["close"]
    move_pct = directional_move_pct(entry, exit_price, direction)
    result_r = move_pct / sl_pct
    bar_end = last_bar["time"] + 300

    return {
        "sl_price": sl_price, "tp_price": tp_price,
        "event": "TIME_EXIT", "exit_price": exit_price,
        "result_r": round(result_r, 8),
        "holding_hours": round((bar_end - open_ts) / 3600, 4),
        "mfe_pct": mfe_pct, "mae_pct": mae_pct,
        "exit_bar_time_jst": ts_to_jst_str(bar_end),
        "note": f"{max_hours}h時間切れ",
    }


def register_new_open(state, strategy, row):
    trade_id = make_trade_id(strategy, row)
    if trade_id in state["pending"]:
        return False

    source_time = row.get("time", "")
    entry = num(row.get("entry"))
    if not source_time or not row.get("symbol") or entry <= 0:
        return False

    open_dt = parse_source_time(source_time)
    trade = {
        "trade_id": trade_id,
        "strategy": strategy,
        "source_time": source_time,
        "symbol": row.get("symbol", ""),
        "direction": row.get("direction", ""),
        "entry": entry,
        "source_sl": num(row.get("sl")),
        "source_tp": num(row.get("tp")),
        "registered_at_jst": datetime.now(JST).isoformat(),
        "open_ts": open_dt.timestamp(),
    }
    for field in SNAPSHOT_FIELDS:
        trade[field] = row.get(field, "")
    trade["snapshot_json"] = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    state["pending"][trade_id] = trade
    append_csv(TRADES_FILE, TRADE_FIELDS, trade)
    return True


def initialize_offsets(state):
    print("EXIT Research 初回初期化")
    for strategy, path in SOURCE_FILES.items():
        rows = read_csv_rows(path)
        state["source_offsets"][path] = len(rows)
        print(strategy, "既存行を基準化:", len(rows))
    state["initialized"] = True
    save_state(state)
    print("初期化完了。次回実行から新規OPENを収集します。")


def ingest_new_source_rows(state):
    added = 0
    for strategy, path in SOURCE_FILES.items():
        rows = read_csv_rows(path)
        old_offset = int(state["source_offsets"].get(path, 0))
        if len(rows) < old_offset:
            print(strategy, "CSV行数減少を検出:", old_offset, "->", len(rows), "再基準化")
            state["source_offsets"][path] = len(rows)
            continue
        for row in rows[old_offset:]:
            if row.get("event") == "OPEN" and register_new_open(state, strategy, row):
                added += 1
        state["source_offsets"][path] = len(rows)
    return added


def process_matured_trades(state):
    now = datetime.now(JST)
    completed = 0
    failed = 0
    required_age = timedelta(hours=24, minutes=PROCESS_GRACE_MINUTES)

    for trade_id in list(state["pending"].keys()):
        trade = state["pending"][trade_id]
        try:
            open_dt = parse_source_time(trade["source_time"])
        except Exception as e:
            print(trade_id, "OPEN時刻解析エラー:", e)
            failed += 1
            continue

        if now - open_dt < required_age:
            continue

        symbol = trade["symbol"]
        try:
            bars = fetch_24h_5m_history(symbol, open_dt)
            if not bars:
                raise RuntimeError("5分足履歴が0件")

            result_count = 0
            for sl_pct in SL_PCTS:
                for rr in RRS:
                    for max_hours in MAX_HOURS_LIST:
                        outcome = evaluate_variant(trade, bars, sl_pct, rr, max_hours)
                        row = {
                            "trade_id": trade_id,
                            "strategy": trade["strategy"],
                            "symbol": symbol,
                            "direction": trade["direction"],
                            "entry": trade["entry"],
                            "sl_pct": sl_pct,
                            "rr": rr,
                            "max_hours": max_hours,
                            **outcome,
                        }
                        append_csv(RESULTS_FILE, RESULT_FIELDS, row)
                        result_count += 1

            if result_count != 60:
                raise RuntimeError(f"出口結果件数が60ではありません: {result_count}")

            del state["pending"][trade_id]
            completed += 1
            print("EXIT Research 完了:", trade_id, trade["strategy"], symbol, "| 60 variants")
        except Exception as e:
            failed += 1
            print("EXIT Research 処理失敗:", trade_id, symbol, e)

    return completed, failed


def main():
    state = load_state()
    if not state.get("initialized"):
        initialize_offsets(state)
        return

    added = ingest_new_source_rows(state)
    completed, failed = process_matured_trades(state)
    save_state(state)

    print()
    print("=" * 80)
    print("MEXC Research Exit")
    print("=" * 80)
    print("新規OPEN登録:", added)
    print("24h評価完了:", completed)
    print("評価失敗:", failed)
    print("24h待機中:", len(state["pending"]))
    print("出口パターン:", len(SL_PCTS) * len(RRS) * len(MAX_HOURS_LIST))
    print("=" * 80)


if __name__ == "__main__":
    main()
