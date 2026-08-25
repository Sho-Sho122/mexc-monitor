import csv
import glob
import json
import os
from datetime import datetime, timedelta, timezone
import requests

# =========================================================
# MEXC HYBRID PRODUCTION v1
# LONG主役: PULLBACK
# SHORT主役: TREND_VOLUME
# 補助: HIGH_EDGE / OI_FUNDING
# Discord通知 + 仮想取引 + MFE/MAE + 定期成績サマリー
# =========================================================

JST = timezone(timedelta(hours=9))

STATE_FILE = "mexc_hybrid_state.json"
TRADE_LOG_FILE = "mexc_hybrid_trades.csv"

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

START_BALANCE = 38.0
RISK_PCT = 0.0145          # 初期38USDTなら約0.55USDT = 1R
RR = 2.0
MAX_OPEN_POSITIONS = 4
LIMIT_EXPIRY_MINUTES = 30

MAX_SPREAD_PCT = 0.05
MIN_AMOUNT24 = 5_000_000

# ランク閾値
A_SCORE = 5
BPLUS_SCORE = 4
B_SCORE = 3

# 成績サマリー
SUMMARY_EVERY_HOURS = 6

TRADE_FIELDS = [
    "time", "symbol", "event", "direction", "rank", "entry_type",
    "entry", "sl", "tp", "exit", "risk_usdt", "pnl_usdt", "result_r",
    "balance", "score_long", "score_short", "reason",
    "mfe_r", "mae_r",
    "hit_plus_0_5r", "hit_plus_1r", "hit_plus_1_5r",
    "hit_plus_2r", "hit_plus_3r",
    "hit_minus_0_5r", "hit_minus_0_75r", "hit_minus_1r"
]


def now_jst():
    return datetime.now(JST)


def now_iso():
    return now_jst().isoformat()


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
        return {
            "balance": START_BALANCE,
            "positions": {},
            "limit_orders": {},
            "last_signal": {},
            "wins": 0,
            "losses": 0,
            "unknown": 0,
            "closed_trades": 0,
            "total_pnl": 0.0,
            "last_scan": None,
            "last_summary_at": None
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return load_state_from_default()

    defaults = load_state_from_default()
    for k, v in defaults.items():
        state.setdefault(k, v)
    return state


def load_state_from_default():
    return {
        "balance": START_BALANCE,
        "positions": {},
        "limit_orders": {},
        "last_signal": {},
        "wins": 0,
        "losses": 0,
        "unknown": 0,
        "closed_trades": 0,
        "total_pnl": 0.0,
        "last_scan": None,
        "last_summary_at": None
    }


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def append_trade(row):
    exists = os.path.exists(TRADE_LOG_FILE)
    with open(TRADE_LOG_FILE, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def discord_send(content=None, embeds=None):
    if not DISCORD_WEBHOOK_URL:
        print("Discord未送信: DISCORD_WEBHOOK_URL がありません")
        return False

    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        if r.status_code not in (200, 204):
            print("Discord送信失敗:", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        print("Discord送信エラー:", e)
        return False


# =========================================================
# 4戦略の判定
# =========================================================

def basic_quality(r):
    if num(r.get("spread_pct"), 999) > MAX_SPREAD_PCT:
        return False
    if num(r.get("amount24")) < MIN_AMOUNT24:
        return False
    return True


def pullback_signal(r):
    if not basic_quality(r):
        return None

    price = num(r.get("price"))
    e9_1 = num(r.get("ema9_1"))
    e9_5 = num(r.get("ema9_5"))
    e21_5 = num(r.get("ema21_5"))
    e9_15 = num(r.get("ema9_15"))
    e21_15 = num(r.get("ema21_15"))
    m1, ms1 = num(r.get("macd1")), num(r.get("macd_signal1"))
    m15, ms15 = num(r.get("macd15")), num(r.get("macd_signal15"))
    rsi1, rsi5, rsi15 = num(r.get("rsi1")), num(r.get("rsi5")), num(r.get("rsi15"))

    if e9_5 <= 0:
        return None

    dist = abs(price - e9_5) / e9_5 * 100
    near_ema = dist <= 0.35

    long_ok = (
        e9_15 > e21_15 and
        m15 > ms15 and
        e9_5 > e21_5 and
        near_ema and
        38 <= rsi5 <= 58 and
        35 <= rsi15 <= 70 and
        price >= e9_1 and
        m1 > ms1 and
        rsi1 >= 45
    )

    short_ok = (
        e9_15 < e21_15 and
        m15 < ms15 and
        e9_5 < e21_5 and
        near_ema and
        42 <= rsi5 <= 62 and
        30 <= rsi15 <= 65 and
        price <= e9_1 and
        m1 < ms1 and
        rsi1 <= 55
    )

    if long_ok:
        return {"direction": "LONG", "ema_distance_pct": dist}
    if short_ok:
        return {"direction": "SHORT", "ema_distance_pct": dist}
    return None


def trend_volume_signal(r):
    if not basic_quality(r):
        return None

    price = num(r.get("price"))
    e9_5, e21_5 = num(r.get("ema9_5")), num(r.get("ema21_5"))
    e9_15, e21_15 = num(r.get("ema9_15")), num(r.get("ema21_15"))
    m5, ms5 = num(r.get("macd5")), num(r.get("macd_signal5"))
    m15, ms15 = num(r.get("macd15")), num(r.get("macd_signal15"))
    v1, v5 = num(r.get("volume_ratio1")), num(r.get("volume_ratio5"))
    ls, ss = num(r.get("long_score")), num(r.get("short_score"))
    edge = abs(ls - ss)

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
        return {"direction": "LONG", "edge": edge, "v1": v1, "v5": v5}
    if short_ok:
        return {"direction": "SHORT", "edge": edge, "v1": v1, "v5": v5}
    return None


def high_edge_signal(r):
    if not basic_quality(r):
        return None

    ls = num(r.get("long_score"))
    ss = num(r.get("short_score"))
    edge = abs(ls - ss)

    if edge < 35:
        return None

    direction = "LONG" if ls > ss else "SHORT"
    rsi15 = num(r.get("rsi15"))

    if direction == "LONG" and rsi15 >= 82:
        return None
    if direction == "SHORT" and rsi15 <= 18:
        return None

    return {"direction": direction, "edge": edge}


def oi_funding_signal(r):
    if not basic_quality(r):
        return None

    oi_change = num(r.get("oi_change_pct"), -999)
    funding = num(r.get("funding_rate"))
    price = num(r.get("price"))
    e9_5 = num(r.get("ema9_5"))
    e9_15 = num(r.get("ema9_15"))
    e21_15 = num(r.get("ema21_15"))
    m5, ms5 = num(r.get("macd5")), num(r.get("macd_signal5"))
    v5 = num(r.get("volume_ratio5"))
    ls, ss = num(r.get("long_score")), num(r.get("short_score"))
    edge = abs(ls - ss)

    if oi_change == -999 or oi_change < 0.30 or v5 < 1.10 or edge < 10:
        return None

    long_ok = (
        price > e9_5 and
        e9_15 > e21_15 and
        m5 > ms5 and
        funding <= 0.00030
    )

    short_ok = (
        price < e9_5 and
        e9_15 < e21_15 and
        m5 < ms5 and
        funding >= -0.00030
    )

    if long_ok:
        return {"direction": "LONG", "oi_change_pct": oi_change, "funding": funding}
    if short_ok:
        return {"direction": "SHORT", "oi_change_pct": oi_change, "funding": funding}
    return None


# =========================================================
# HYBRIDスコア
# =========================================================

def evaluate_hybrid(r):
    pb = pullback_signal(r)
    tv = trend_volume_signal(r)
    he = high_edge_signal(r)
    oi = oi_funding_signal(r)

    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    # 主役
    if pb and pb["direction"] == "LONG":
        long_score += 3
        reasons_long.append("PULLBACK LONG")
    elif pb and pb["direction"] == "SHORT":
        short_score += 1
        reasons_short.append("PULLBACK SHORT(補助)")

    if tv and tv["direction"] == "SHORT":
        short_score += 3
        reasons_short.append("TREND_VOLUME SHORT")
    elif tv and tv["direction"] == "LONG":
        long_score += 1
        reasons_long.append("TREND_VOLUME LONG(補助)")

    # 補助
    if he:
        if he["direction"] == "LONG":
            long_score += 1
            reasons_long.append(f"HIGH_EDGE LONG({he['edge']:.1f})")
        else:
            # HIGH_EDGE SHORT単独成績が悪かったので弱い補助に留める
            short_score += 0.5
            reasons_short.append(f"HIGH_EDGE SHORT({he['edge']:.1f},弱補助)")

    if oi:
        if oi["direction"] == "LONG":
            long_score += 1
            reasons_long.append(f"OI LONG({oi['oi_change_pct']:.2f}%)")
        else:
            short_score += 1
            reasons_short.append(f"OI SHORT({oi['oi_change_pct']:.2f}%)")

    # 取引品質は記録する。v1では強制最適化しすぎない。
    spread = num(r.get("spread_pct"))
    amount24 = num(r.get("amount24"))
    v1 = num(r.get("volume_ratio1"))
    v5 = num(r.get("volume_ratio5"))
    rsi5 = num(r.get("rsi5"))
    ema_dist = None
    if pb:
        ema_dist = pb.get("ema_distance_pct")

    if long_score > short_score:
        direction = "LONG"
        score = long_score
        opposite = short_score
        reasons = reasons_long
    elif short_score > long_score:
        direction = "SHORT"
        score = short_score
        opposite = long_score
        reasons = reasons_short
    else:
        direction = "NEUTRAL"
        score = long_score
        opposite = short_score
        reasons = []

    # 主戦略が無い方向はAにしない
    has_primary = (
        (direction == "LONG" and pb and pb["direction"] == "LONG") or
        (direction == "SHORT" and tv and tv["direction"] == "SHORT")
    )

    # 主戦略同士が逆方向の場合は補助が十分でない限り見送る
    primary_conflict = bool(
        pb and tv and pb["direction"] != tv["direction"]
    )

    if direction == "NEUTRAL" or not has_primary:
        rank = "C"
    elif primary_conflict and abs(long_score - short_score) < 2:
        rank = "C"
    elif score >= A_SCORE and (score - opposite) >= 2:
        rank = "A"
    elif score >= BPLUS_SCORE and (score - opposite) >= 1.5:
        rank = "B+"
    elif score >= B_SCORE and (score - opposite) >= 1:
        rank = "B"
    else:
        rank = "C"

    return {
        "symbol": r.get("symbol"),
        "price": num(r.get("price")),
        "direction": direction,
        "rank": rank,
        "score_long": long_score,
        "score_short": short_score,
        "reason": " / ".join(reasons) if reasons else "条件不足または矛盾",
        "spread_pct": spread,
        "amount24": amount24,
        "volume_ratio1": v1,
        "volume_ratio5": v5,
        "rsi5": rsi5,
        "ema_distance_pct": ema_dist,
        "atr5_pct": num(r.get("atr5_pct")),
        "pb": pb,
        "tv": tv,
        "he": he,
        "oi": oi,
    }


def rank_key(rank):
    return {"A": 4, "B+": 3, "B": 2, "C": 1}.get(rank, 0)


# =========================================================
# TP/SLと仮想取引
# =========================================================

def make_plan(price, atr5_pct, direction):
    atr_frac = max(atr5_pct, 0.10) / 100.0
    stop_pct = max(atr_frac * 1.20, 0.0035)

    if direction == "LONG":
        sl = price * (1 - stop_pct)
        tp = price * (1 + stop_pct * RR)
    else:
        sl = price * (1 + stop_pct)
        tp = price * (1 - stop_pct * RR)

    return sl, tp


def current_risk_usdt(state):
    return max(0.10, state["balance"] * RISK_PCT)


def can_open_position(state, symbol):
    if symbol in state["positions"]:
        return False
    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return False
    return True


def new_position(symbol, sig, entry, entry_type, state):
    sl, tp = make_plan(entry, sig["atr5_pct"], sig["direction"])
    risk_usdt = current_risk_usdt(state)
    risk_price = abs(entry - sl)

    p = {
        "symbol": symbol,
        "direction": sig["direction"],
        "rank": sig["rank"],
        "entry_type": entry_type,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "risk_usdt": risk_usdt,
        "risk_price": risk_price,
        "opened_at": now_iso(),
        "score_long": sig["score_long"],
        "score_short": sig["score_short"],
        "reason": sig["reason"],
        "mfe_r": 0.0,
        "mae_r": 0.0,
        "levels": {
            "plus_0_5r": None,
            "plus_1r": None,
            "plus_1_5r": None,
            "plus_2r": None,
            "plus_3r": None,
            "minus_0_5r": None,
            "minus_0_75r": None,
            "minus_1r": None
        }
    }

    state["positions"][symbol] = p

    append_trade({
        "time": now_iso(),
        "symbol": symbol,
        "event": "OPEN",
        "direction": p["direction"],
        "rank": p["rank"],
        "entry_type": entry_type,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "risk_usdt": risk_usdt,
        "balance": state["balance"],
        "score_long": p["score_long"],
        "score_short": p["score_short"],
        "reason": p["reason"],
        "mfe_r": 0,
        "mae_r": 0
    })


def r_excursions(p, high, low):
    risk = p["risk_price"]
    if risk <= 0:
        return 0.0, 0.0

    if p["direction"] == "LONG":
        favorable = (high - p["entry"]) / risk
        adverse = (p["entry"] - low) / risk
    else:
        favorable = (p["entry"] - low) / risk
        adverse = (high - p["entry"]) / risk

    return favorable, adverse


def mark_level_hits(p, favorable, adverse):
    t = now_iso()
    levels = p["levels"]

    plus_levels = [
        ("plus_0_5r", 0.5),
        ("plus_1r", 1.0),
        ("plus_1_5r", 1.5),
        ("plus_2r", 2.0),
        ("plus_3r", 3.0),
    ]
    minus_levels = [
        ("minus_0_5r", 0.5),
        ("minus_0_75r", 0.75),
        ("minus_1r", 1.0),
    ]

    for key, level in plus_levels:
        if levels.get(key) is None and favorable >= level:
            levels[key] = t

    for key, level in minus_levels:
        if levels.get(key) is None and adverse >= level:
            levels[key] = t


def close_position(state, symbol, event, exit_price, result_r):
    p = state["positions"][symbol]
    pnl = p["risk_usdt"] * result_r if result_r != "" else 0.0

    if event == "WIN":
        state["wins"] += 1
        state["closed_trades"] += 1
        state["total_pnl"] += pnl
        state["balance"] += pnl
    elif event == "LOSS":
        state["losses"] += 1
        state["closed_trades"] += 1
        state["total_pnl"] += pnl
        state["balance"] += pnl
    else:
        state["unknown"] += 1

    lv = p["levels"]

    append_trade({
        "time": now_iso(),
        "symbol": symbol,
        "event": event,
        "direction": p["direction"],
        "rank": p["rank"],
        "entry_type": p["entry_type"],
        "entry": p["entry"],
        "sl": p["sl"],
        "tp": p["tp"],
        "exit": exit_price,
        "risk_usdt": p["risk_usdt"],
        "pnl_usdt": pnl if event in ("WIN", "LOSS") else "",
        "result_r": result_r,
        "balance": state["balance"],
        "score_long": p["score_long"],
        "score_short": p["score_short"],
        "reason": p["reason"],
        "mfe_r": round(p["mfe_r"], 4),
        "mae_r": round(p["mae_r"], 4),
        "hit_plus_0_5r": lv.get("plus_0_5r"),
        "hit_plus_1r": lv.get("plus_1r"),
        "hit_plus_1_5r": lv.get("plus_1_5r"),
        "hit_plus_2r": lv.get("plus_2r"),
        "hit_plus_3r": lv.get("plus_3r"),
        "hit_minus_0_5r": lv.get("minus_0_5r"),
        "hit_minus_0_75r": lv.get("minus_0_75r"),
        "hit_minus_1r": lv.get("minus_1r")
    })

    del state["positions"][symbol]


def update_positions(state, rows):
    by_symbol = {r["symbol"]: r for r in rows}

    for symbol in list(state["positions"].keys()):
        p = state["positions"][symbol]
        r = by_symbol.get(symbol)
        if not r:
            continue

        current = num(r.get("price"))
        high5 = num(r.get("last_high5"), current)
        low5 = num(r.get("last_low5"), current)

        favorable, adverse = r_excursions(p, high5, low5)
        p["mfe_r"] = max(p.get("mfe_r", 0.0), favorable)
        p["mae_r"] = max(p.get("mae_r", 0.0), adverse)
        mark_level_hits(p, favorable, adverse)

        if p["direction"] == "LONG":
            hit_tp = high5 >= p["tp"]
            hit_sl = low5 <= p["sl"]
        else:
            hit_tp = low5 <= p["tp"]
            hit_sl = high5 >= p["sl"]

        # 同じ確定5分足内で両方なら順序不明
        if hit_tp and hit_sl:
            close_position(state, symbol, "UNKNOWN", "", "")
        elif hit_tp:
            close_position(state, symbol, "WIN", p["tp"], RR)
        elif hit_sl:
            close_position(state, symbol, "LOSS", p["sl"], -1.0)


def update_limit_orders(state, rows, signals):
    by_symbol = {r["symbol"]: r for r in rows}

    for symbol in list(state["limit_orders"].keys()):
        order = state["limit_orders"][symbol]

        try:
            created = datetime.fromisoformat(order["created_at"])
        except Exception:
            created = now_jst()

        if now_jst() - created > timedelta(minutes=LIMIT_EXPIRY_MINUTES):
            del state["limit_orders"][symbol]
            continue

        r = by_symbol.get(symbol)
        sig = signals.get(symbol)

        if not r or not sig:
            continue

        current = num(r.get("price"))
        high5 = num(r.get("last_high5"), current)
        low5 = num(r.get("last_low5"), current)
        limit_price = order["limit_price"]

        if order["direction"] == "LONG":
            filled = low5 <= limit_price
        else:
            filled = high5 >= limit_price

        if filled and can_open_position(state, symbol):
            new_position(symbol, sig, limit_price, "LIMIT", state)
            del state["limit_orders"][symbol]


def maybe_create_virtual_trade(state, sig):
    symbol = sig["symbol"]

    if sig["rank"] == "A":
        if can_open_position(state, symbol):
            new_position(symbol, sig, sig["price"], "A_MARKET", state)

    elif sig["rank"] == "B+":
        if symbol in state["positions"] or symbol in state["limit_orders"]:
            return

        # 押し目/戻りを待つ簡易指値: 5m EMA9付近
        # sigにはEMA値を持たせていないので現在価格から0.15%戻した価格を使う
        if sig["direction"] == "LONG":
            limit_price = sig["price"] * (1 - 0.0015)
        else:
            limit_price = sig["price"] * (1 + 0.0015)

        state["limit_orders"][symbol] = {
            "direction": sig["direction"],
            "rank": sig["rank"],
            "limit_price": limit_price,
            "created_at": now_iso()
        }


# =========================================================
# Discord
# =========================================================

def signal_signature(sig):
    return f"{sig['rank']}:{sig['direction']}"


def should_notify(prev, current):
    if prev is None:
        return current["rank"] in ("A", "B+", "B")

    prev_rank = prev.get("rank", "C")
    prev_dir = prev.get("direction", "NEUTRAL")
    cur_rank = current["rank"]
    cur_dir = current["direction"]

    if prev_rank == "B" and cur_rank == "B":
        return False
    if prev_rank == "B" and cur_rank == "B+":
        return True
    if prev_rank == "B+" and cur_rank == "A":
        return True
    if prev_rank == "A" and cur_rank == "A" and prev_dir == cur_dir:
        return False
    if prev_rank == "A" and cur_rank == "A" and prev_dir != cur_dir:
        return True
    if prev_rank == "A" and cur_rank == "C":
        return True

    return signal_signature(prev) != signal_signature(current)


def discord_signal(sig, prev=None):
    rank = sig["rank"]
    direction = sig["direction"]

    if rank == "C":
        if prev and prev.get("rank") == "A":
            discord_send(
                f"⚪ **{sig['symbol']}** Aシグナル失効 → C"
            )
        return

    if rank == "B":
        discord_send(
            f"**B {direction} | {sig['symbol']}** | "
            f"価格 {sig['price']:.8g} | "
            f"L{sig['score_long']:.1f}/S{sig['score_short']:.1f}"
        )
        return

    sl, tp = make_plan(sig["price"], sig["atr5_pct"], direction)
    color = 0x2ECC71 if direction == "LONG" else 0xE74C3C

    title = f"{rank} {direction} | {sig['symbol']}"
    description = (
        f"**現在価格:** {sig['price']:.8g}\n"
        f"**仮想Entry:** {sig['price']:.8g}\n"
        f"**TP:** {tp:.8g}\n"
        f"**SL:** {sl:.8g}\n"
        f"**Score:** L {sig['score_long']:.1f} / S {sig['score_short']:.1f}\n"
        f"**理由:** {sig['reason']}\n"
        f"Spread {sig['spread_pct']:.4f}% | "
        f"Vol1 {sig['volume_ratio1']:.2f} | Vol5 {sig['volume_ratio5']:.2f}"
    )

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    discord_send(embeds=[embed])


def summary_due(state):
    raw = state.get("last_summary_at")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
        return now_jst() - last >= timedelta(hours=SUMMARY_EVERY_HOURS)
    except Exception:
        return True


def send_summary(state):
    closed = state["closed_trades"]
    wins = state["wins"]
    losses = state["losses"]
    winrate = (wins / closed * 100) if closed else 0.0
    roi = ((state["balance"] / START_BALANCE) - 1) * 100

    msg = (
        "📊 **HYBRID 仮想取引サマリー**\n"
        f"残高: **{state['balance']:.4f} USDT** "
        f"({roi:+.2f}%)\n"
        f"決着: {closed} | 勝 {wins} / 負 {losses} "
        f"| 勝率 {winrate:.1f}%\n"
        f"累計PnL: **{state['total_pnl']:+.4f} USDT**\n"
        f"保有中: {len(state['positions'])} | "
        f"未約定指値: {len(state['limit_orders'])}"
    )

    discord_send(msg)
    state["last_summary_at"] = now_iso()


# =========================================================
# Main
# =========================================================

def main():
    scan = latest_scan()
    scan_name = os.path.basename(scan)
    state = load_state()

    if state.get("last_scan") == scan_name:
        print("同じCSVは処理済み:", scan_name)
        return

    with open(scan, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    signals = {}
    for r in rows:
        sig = evaluate_hybrid(r)
        sig["atr5_pct"] = num(r.get("atr5_pct"))
        signals[sig["symbol"]] = sig

    # 既存ポジション/指値を先に処理
    update_positions(state, rows)
    update_limit_orders(state, rows, signals)

    # ランクの高い順に通知・仮想取引
    ordered = sorted(
        signals.values(),
        key=lambda x: (rank_key(x["rank"]), abs(x["score_long"] - x["score_short"])),
        reverse=True
    )

    for sig in ordered:
        symbol = sig["symbol"]
        prev = state["last_signal"].get(symbol)

        if should_notify(prev, sig):
            discord_signal(sig, prev)

        if sig["rank"] in ("A", "B+"):
            maybe_create_virtual_trade(state, sig)

        state["last_signal"][symbol] = {
            "rank": sig["rank"],
            "direction": sig["direction"],
            "time": now_iso()
        }

    if summary_due(state):
        send_summary(state)

    state["last_scan"] = scan_name
    save_state(state)

    counts = {"A": 0, "B+": 0, "B": 0, "C": 0}
    for s in signals.values():
        counts[s["rank"]] = counts.get(s["rank"], 0) + 1

    print("HYBRID完了")
    print("使用CSV:", scan_name)
    print("A:", counts["A"], "B+:", counts["B+"], "B:", counts["B"], "C:", counts["C"])
    print("仮想残高:", round(state["balance"], 4))
    print("保有:", len(state["positions"]), "指値:", len(state["limit_orders"]))


if __name__ == "__main__":
    main()
