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

def append_log(row):
    fields = [
        "time","symbol","event","direction","edge","entry","sl","tp",
        "exit","result_r","rsi1","rsi5","rsi15","spread_pct",
        "amount24","volume_ratio1","volume_ratio5","note"
    ]
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

        append_log({
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
        })

def main():
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
