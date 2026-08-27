import csv
import glob
import json
import os
from datetime import datetime

RR = 2.0
MAX_SPREAD_PCT = 0.05
MIN_AMOUNT24 = 5_000_000

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

def load_state(path):
    if not os.path.exists(path):
        return {"positions": {}, "last_scan": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"positions": {}, "last_scan": None}

def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def append_log(path, fields, row):
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})

def ensure_csv_schema(path, fields):
    """既存CSVを保持したまま、新しいヘッダーへ一度だけ移行する。"""
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        old_fields = reader.fieldnames or []
        if old_fields == fields:
            return
        old_rows = list(reader)

    tmp = path + ".schema_tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in old_rows:
            writer.writerow({k: row.get(k, "") for k in fields})

    os.replace(tmp, path)
    print("TREND_VOLUME CSV schema updated:", len(old_fields), "->", len(fields), "columns")


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
        "rsi1": num(r.get("rsi1")),
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
    }


def make_plan(r, direction):
    price = num(r.get("price"))
    atr5_pct = max(num(r.get("atr5_pct")), 0.10) / 100.0
    stop_pct = max(atr5_pct * 1.20, 0.0035)

    if direction == "LONG":
        sl = price * (1 - stop_pct)
        tp = price * (1 + stop_pct * RR)
    else:
        sl = price * (1 + stop_pct)
        tp = price * (1 - stop_pct * RR)

    return price, sl, tp

def update_positions(state, rows, log_file, fields):
    rows_by_symbol = {r["symbol"]: r for r in rows}

    for symbol in list(state["positions"].keys()):
        p = state["positions"][symbol]
        r = rows_by_symbol.get(symbol)
        if not r:
            continue

        current = num(r.get("price"))
        high5 = num(r.get("last_high5"), current)
        low5 = num(r.get("last_low5"), current)

        if p["direction"] == "LONG":
            hit_tp = high5 >= p["tp"]
            hit_sl = low5 <= p["sl"]
        else:
            hit_tp = low5 <= p["tp"]
            hit_sl = high5 >= p["sl"]

        base = {
            "time": datetime.now().isoformat(),
            "symbol": symbol,
            "direction": p["direction"],
            "entry": p["entry"],
            "sl": p["sl"],
            "tp": p["tp"],
        }

        if hit_tp and hit_sl:
            base.update({
                "event": "UNKNOWN",
                "exit": "",
                "result_r": "",
                "note": "同一5分足でTP/SL双方到達・順序不明"
            })
            append_log(log_file, fields, base)
            del state["positions"][symbol]

        elif hit_tp:
            base.update({
                "event": "WIN",
                "exit": p["tp"],
                "result_r": RR,
                "note": "TP"
            })
            append_log(log_file, fields, base)
            del state["positions"][symbol]

        elif hit_sl:
            base.update({
                "event": "LOSS",
                "exit": p["sl"],
                "result_r": -1.0,
                "note": "SL"
            })
            append_log(log_file, fields, base)
            del state["positions"][symbol]

STATE_FILE = "strategy_trend_volume_state.json"
LOG_FILE = "strategy_trend_volume.csv"

FIELDS = [
    # 既存列
    "time","symbol","event","direction","entry","sl","tp","exit","result_r",
    "edge","rsi5","rsi15","volume_ratio1","volume_ratio5","spread_pct","amount24","note",

    # 共通特徴量
    "scan_time_jst","ticker_time_jst",
    "price","bid","ask","volume24","change24",
    "funding_rate","oi","oi_change_pct",
    "rsi1",
    "ema9_1","ema21_1","ema9_5","ema21_5","ema9_15","ema21_15",
    "macd1","macd_signal1","macd_hist1",
    "macd5","macd_signal5","macd_hist5",
    "macd15","macd_signal15","macd_hist15",
    "volume_ratio15",
    "atr1_pct","atr5_pct","atr15_pct",
    "long_score","short_score","bias"
]

def qualifies(r):
    price = num(r.get("price"))
    e9_5, e21_5 = num(r.get("ema9_5")), num(r.get("ema21_5"))
    e9_15, e21_15 = num(r.get("ema9_15")), num(r.get("ema21_15"))
    m5, ms5 = num(r.get("macd5")), num(r.get("macd_signal5"))
    m15, ms15 = num(r.get("macd15")), num(r.get("macd_signal15"))
    v1, v5 = num(r.get("volume_ratio1")), num(r.get("volume_ratio5"))
    ls, ss = num(r.get("long_score")), num(r.get("short_score"))
    edge = abs(ls - ss)

    if num(r.get("spread_pct"), 999) > MAX_SPREAD_PCT:
        return False, "NEUTRAL", edge
    if num(r.get("amount24")) < MIN_AMOUNT24:
        return False, "NEUTRAL", edge

    long_ok = (
        price > e9_5 > e21_5 and
        e9_15 > e21_15 and
        m5 > ms5 and
        m15 > ms15 and
        v5 >= 1.50 and
        v1 >= 1.00 and
        edge >= 12
    )

    short_ok = (
        price < e9_5 < e21_5 and
        e9_15 < e21_15 and
        m5 < ms5 and
        m15 < ms15 and
        v5 >= 1.50 and
        v1 >= 1.00 and
        edge >= 12
    )

    if long_ok:
        return True, "LONG", edge
    if short_ok:
        return True, "SHORT", edge
    return False, "NEUTRAL", edge

def main():
    ensure_csv_schema(LOG_FILE, FIELDS)
    scan = latest_scan()
    scan_name = os.path.basename(scan)
    state = load_state(STATE_FILE)

    if state.get("last_scan") == scan_name:
        print("同じCSVは処理済み:", scan_name)
        return

    with open(scan, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    update_positions(state, rows, LOG_FILE, FIELDS)

    for r in rows:
        symbol = r["symbol"]
        if symbol in state["positions"]:
            continue

        ok, direction, edge = qualifies(r)
        if not ok:
            continue

        entry, sl, tp = make_plan(r, direction)
        state["positions"][symbol] = {
            "direction": direction,
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
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "edge": edge,
            "rsi5": num(r.get("rsi5")),
            "rsi15": num(r.get("rsi15")),
            "volume_ratio1": num(r.get("volume_ratio1")),
            "volume_ratio5": num(r.get("volume_ratio5")),
            "spread_pct": num(r.get("spread_pct")),
            "amount24": num(r.get("amount24")),
            "note": "TREND_VOLUME"
        }
        row.update(common_snapshot(r))
        append_log(LOG_FILE, FIELDS, row)

    state["last_scan"] = scan_name
    save_state(STATE_FILE, state)

    print("TREND_VOLUME 完了")
    print("使用CSV:", scan_name)
    print("保有中:", len(state["positions"]))

if __name__ == "__main__":
    main()
