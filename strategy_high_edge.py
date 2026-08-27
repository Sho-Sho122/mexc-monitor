import csv
import glob
import json
import os
from datetime import datetime

# =========================================================
# HIGH_EDGE research strategy
# - Uses latest mexc_scan_*.csv
# - No capital / correlation limits
# - One open trade per symbol
# - Fixed normalized result: WIN = +2R, LOSS = -1R
# =========================================================

EDGE_MIN = 35.0
MAX_SPREAD_PCT = 0.05
MIN_AMOUNT24 = 5_000_000
RR = 2.0

STATE_FILE = "strategy_high_edge_state.json"
LOG_FILE = "strategy_high_edge.csv"

FIELDS = [
    # 既存列
    "time","symbol","event","direction","edge","entry","sl","tp",
    "exit","result_r","rsi1","rsi5","rsi15","spread_pct",
    "amount24","volume_ratio1","volume_ratio5","note",

    # 共通特徴量
    "scan_time_jst","ticker_time_jst",
    "price","bid","ask","volume24","change24",
    "funding_rate","oi","oi_change_pct",
    "ema9_1","ema21_1","ema9_5","ema21_5","ema9_15","ema21_15",
    "macd1","macd_signal1","macd_hist1",
    "macd5","macd_signal5","macd_hist5",
    "macd15","macd_signal15","macd_hist15",
    "volume_ratio15",
    "atr1_pct","atr5_pct","atr15_pct",
    "long_score","short_score","bias",

    # BTC市場状態（Scannerから転記）
    "btc_price",
    "btc_ret_1m_pct",
    "btc_ret_5m_pct",
    "btc_ret_15m_pct",
    "btc_ret_1h_pct",
    "btc_rsi1",
    "btc_rsi5",
    "btc_rsi15",
    "btc_rsi60",
    "btc_ema9_1",
    "btc_ema21_1",
    "btc_ema9_5",
    "btc_ema21_5",
    "btc_ema9_15",
    "btc_ema21_15",
    "btc_ema9_60",
    "btc_ema21_60",
    "btc_macd1",
    "btc_macd_signal1",
    "btc_macd5",
    "btc_macd_signal5",
    "btc_macd15",
    "btc_macd_signal15",
    "btc_macd60",
    "btc_macd_signal60",
    "btc_volume_ratio1",
    "btc_volume_ratio5",
    "btc_volume_ratio15",
    "btc_volume_ratio60",
    "btc_atr1_pct",
    "btc_atr5_pct",
    "btc_atr15_pct",
    "btc_atr60_pct",
]


def num(x, default=0.0):
    try:
        if x in ("", None, "None", "nan", "NaN"):
            return default
        return float(x)
    except Exception:
        return default

def latest_scan():
    files = glob.glob("mexc_scan_*.csv")
    if not files:
        raise FileNotFoundError("mexc_scan_*.csv が見つかりません")
    return max(files, key=os.path.getmtime)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"positions": {}, "last_scan": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"positions": {}, "last_scan": None}

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def ensure_csv_schema():
    """既存CSVを保持したまま、新しいヘッダーへ一度だけ移行する。"""
    if not os.path.exists(LOG_FILE):
        return

    with open(LOG_FILE, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        old_fields = reader.fieldnames or []
        if old_fields == FIELDS:
            return
        old_rows = list(reader)

    tmp = LOG_FILE + ".schema_tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in old_rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})

    os.replace(tmp, LOG_FILE)
    print("HIGH_EDGE CSV schema updated:", len(old_fields), "->", len(FIELDS), "columns")


def common_snapshot(r):
    return {
        "scan_time_jst": r.get("scan_time_jst", ""),
        "ticker_time_jst": r.get("ticker_time_jst", ""),
        "price": num(r.get("price")),
        "bid": num(r.get("bid")),
        "ask": num(r.get("ask")),
        "volume24": num(r.get("volume24")),
        "change24": num(r.get("change24")),
        "funding_rate": num(r.get("funding_rate")),
        "oi": num(r.get("oi")),
        "oi_change_pct": (
            "" if r.get("oi_change_pct") in ("", None, "None", "nan", "NaN")
            else num(r.get("oi_change_pct"))
        ),
        "ema9_1": num(r.get("ema9_1")),
        "ema21_1": num(r.get("ema21_1")),
        "ema9_5": num(r.get("ema9_5")),
        "ema21_5": num(r.get("ema21_5")),
        "ema9_15": num(r.get("ema9_15")),
        "ema21_15": num(r.get("ema21_15")),
        "macd1": num(r.get("macd1")),
        "macd_signal1": num(r.get("macd_signal1")),
        "macd_hist1": num(r.get("macd_hist1")),
        "macd5": num(r.get("macd5")),
        "macd_signal5": num(r.get("macd_signal5")),
        "macd_hist5": num(r.get("macd_hist5")),
        "macd15": num(r.get("macd15")),
        "macd_signal15": num(r.get("macd_signal15")),
        "macd_hist15": num(r.get("macd_hist15")),
        "volume_ratio15": num(r.get("volume_ratio15")),
        "atr1_pct": num(r.get("atr1_pct")),
        "atr5_pct": num(r.get("atr5_pct")),
        "atr15_pct": num(r.get("atr15_pct")),
        "long_score": num(r.get("long_score")),
        "short_score": num(r.get("short_score")),
        "bias": r.get("bias", ""),

        # BTC市場状態
        "btc_price": num(r.get("btc_price")),
        "btc_ret_1m_pct": num(r.get("btc_ret_1m_pct")),
        "btc_ret_5m_pct": num(r.get("btc_ret_5m_pct")),
        "btc_ret_15m_pct": num(r.get("btc_ret_15m_pct")),
        "btc_ret_1h_pct": num(r.get("btc_ret_1h_pct")),
        "btc_rsi1": num(r.get("btc_rsi1")),
        "btc_rsi5": num(r.get("btc_rsi5")),
        "btc_rsi15": num(r.get("btc_rsi15")),
        "btc_rsi60": num(r.get("btc_rsi60")),
        "btc_ema9_1": num(r.get("btc_ema9_1")),
        "btc_ema21_1": num(r.get("btc_ema21_1")),
        "btc_ema9_5": num(r.get("btc_ema9_5")),
        "btc_ema21_5": num(r.get("btc_ema21_5")),
        "btc_ema9_15": num(r.get("btc_ema9_15")),
        "btc_ema21_15": num(r.get("btc_ema21_15")),
        "btc_ema9_60": num(r.get("btc_ema9_60")),
        "btc_ema21_60": num(r.get("btc_ema21_60")),
        "btc_macd1": num(r.get("btc_macd1")),
        "btc_macd_signal1": num(r.get("btc_macd_signal1")),
        "btc_macd5": num(r.get("btc_macd5")),
        "btc_macd_signal5": num(r.get("btc_macd_signal5")),
        "btc_macd15": num(r.get("btc_macd15")),
        "btc_macd_signal15": num(r.get("btc_macd_signal15")),
        "btc_macd60": num(r.get("btc_macd60")),
        "btc_macd_signal60": num(r.get("btc_macd_signal60")),
        "btc_volume_ratio1": num(r.get("btc_volume_ratio1")),
        "btc_volume_ratio5": num(r.get("btc_volume_ratio5")),
        "btc_volume_ratio15": num(r.get("btc_volume_ratio15")),
        "btc_volume_ratio60": num(r.get("btc_volume_ratio60")),
        "btc_atr1_pct": num(r.get("btc_atr1_pct")),
        "btc_atr5_pct": num(r.get("btc_atr5_pct")),
        "btc_atr15_pct": num(r.get("btc_atr15_pct")),
        "btc_atr60_pct": num(r.get("btc_atr60_pct")),
    }


def append_log(row):
    fields = FIELDS
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})

def qualifies(r):
    long_score = num(r.get("long_score"))
    short_score = num(r.get("short_score"))
    edge = abs(long_score - short_score)
    direction = "LONG" if long_score > short_score else "SHORT"

    if edge < EDGE_MIN:
        return False, direction, edge

    if num(r.get("spread_pct"), 999) > MAX_SPREAD_PCT:
        return False, direction, edge

    if num(r.get("amount24")) < MIN_AMOUNT24:
        return False, direction, edge

    # Avoid the most extreme exhaustion zone.
    rsi15 = num(r.get("rsi15"))
    if direction == "LONG" and rsi15 >= 82:
        return False, direction, edge
    if direction == "SHORT" and rsi15 <= 18:
        return False, direction, edge

    return True, direction, edge

def make_plan(r, direction):
    price = num(r.get("price"))
    atr5_pct = max(num(r.get("atr5_pct")), 0.10) / 100.0

    # Same risk logic for both sides.
    stop_pct = max(atr5_pct * 1.20, 0.0035)

    if direction == "LONG":
        sl = price * (1 - stop_pct)
        tp = price * (1 + stop_pct * RR)
    else:
        sl = price * (1 + stop_pct)
        tp = price * (1 - stop_pct * RR)

    return price, sl, tp

def update_positions(state, rows):
    rows_by_symbol = {r["symbol"]: r for r in rows}

    for symbol in list(state["positions"].keys()):
        p = state["positions"][symbol]
        r = rows_by_symbol.get(symbol)
        if not r:
            continue

        current = num(r.get("price"))

        # Prefer exact last completed 5m bar if scanner provides it.
        high5 = num(r.get("last_high5"), current)
        low5 = num(r.get("last_low5"), current)

        # Fallback for older scanner CSVs:
        # use current price only, which is conservative but can miss intrabar touches.
        if "last_high5" not in r:
            high5 = current
        if "last_low5" not in r:
            low5 = current

        if p["direction"] == "LONG":
            hit_tp = high5 >= p["tp"]
            hit_sl = low5 <= p["sl"]
        else:
            hit_tp = low5 <= p["tp"]
            hit_sl = high5 >= p["sl"]

        if hit_tp and hit_sl:
            append_log({
                "time": datetime.now().isoformat(),
                "symbol": symbol,
                "event": "UNKNOWN",
                "direction": p["direction"],
                "edge": p["edge"],
                "entry": p["entry"],
                "sl": p["sl"],
                "tp": p["tp"],
                "exit": "",
                "result_r": "",
                "note": "同一5分足でTP/SL双方到達・順序不明"
            })
            del state["positions"][symbol]

        elif hit_tp:
            append_log({
                "time": datetime.now().isoformat(),
                "symbol": symbol,
                "event": "WIN",
                "direction": p["direction"],
                "edge": p["edge"],
                "entry": p["entry"],
                "sl": p["sl"],
                "tp": p["tp"],
                "exit": p["tp"],
                "result_r": RR,
                "note": "TP"
            })
            del state["positions"][symbol]

        elif hit_sl:
            append_log({
                "time": datetime.now().isoformat(),
                "symbol": symbol,
                "event": "LOSS",
                "direction": p["direction"],
                "edge": p["edge"],
                "entry": p["entry"],
                "sl": p["sl"],
                "tp": p["tp"],
                "exit": p["sl"],
                "result_r": -1.0,
                "note": "SL"
            })
            del state["positions"][symbol]

def open_new_signals(state, rows):
    for r in rows:
        symbol = r["symbol"]

        # One active HIGH_EDGE trade per symbol.
        if symbol in state["positions"]:
            continue

        ok, direction, edge = qualifies(r)
        if not ok:
            continue

        entry, sl, tp = make_plan(r, direction)

        state["positions"][symbol] = {
            "direction": direction,
            "edge": edge,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "opened_at": datetime.now().isoformat()
        }

        row = {
            "time": datetime.now().isoformat(),
            "symbol": symbol,
            "event": "OPEN",
            "direction": direction,
            "edge": edge,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rsi1": num(r.get("rsi1")),
            "rsi5": num(r.get("rsi5")),
            "rsi15": num(r.get("rsi15")),
            "spread_pct": num(r.get("spread_pct")),
            "amount24": num(r.get("amount24")),
            "volume_ratio1": num(r.get("volume_ratio1")),
            "volume_ratio5": num(r.get("volume_ratio5")),
            "note": "HIGH_EDGE"
        }
        row.update(common_snapshot(r))
        append_log(row)

def main():
    ensure_csv_schema()
    scan = latest_scan()
    scan_name = os.path.basename(scan)
    state = load_state()

    if state.get("last_scan") == scan_name:
        print("同じCSVは処理済み:", scan_name)
        return

    with open(scan, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    # Existing trades settle first; then new opportunities can open.
    update_positions(state, rows)
    open_new_signals(state, rows)

    state["last_scan"] = scan_name
    save_state(state)

    print("HIGH_EDGE 完了")
    print("使用CSV:", scan_name)
    print("保有中:", len(state["positions"]))
    print("ログ:", LOG_FILE)

if __name__ == "__main__":
    main()
