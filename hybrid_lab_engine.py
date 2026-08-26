import csv
import glob
import json
import os
import time
from datetime import datetime, timezone, timedelta

# =========================================================
# HYBRID LAB ENGINE
# 511モデルを1回の市場スキャンで同時評価する研究用エンジン
#
# 設計:
# - 基礎シグナル: PULLBACK / TREND_VOLUME / HIGH_EDGE / OI_FUNDING
# - モデルは weights / primary_multipliers / interactions だけ変える
# - 7系統 + BASE を同一データで比較
# - 1モデルあたり最大4ポジション
# - RR=2:1固定
# - MFE/MAEを記録
# - Discordは「全モデルの合意が強いBEST 1件」のみ（設定でON）
# =========================================================

JST = timezone(timedelta(hours=9))

CONFIG_FILE = "hybrid_lab_models.json"
STATE_FILE = "hybrid_lab_state.json"
TRADE_FILE = "hybrid_lab_trades.csv"
SUMMARY_FILE = "hybrid_lab_model_summary.csv"

RR = 2.0
MAX_OPEN_PER_MODEL = 4
MAX_SPREAD_PCT = 0.05
MIN_AMOUNT24 = 5_000_000

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_LAB_WEBHOOK_URL", "").strip()

TRADE_FIELDS = [
    "time","model_id","family","symbol","event","direction",
    "entry","sl","tp","exit","result_r",
    "score_long","score_short","margin","reason",
    "mfe_r","mae_r"
]


def now_iso():
    return datetime.now(JST).isoformat()


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


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_trade(row):
    exists = os.path.exists(TRADE_FILE)
    with open(TRADE_FILE, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    models = cfg.get("models", [])
    if not cfg.get("active", False):
        print("HYBRID LABは準備状態です。config active=false")
        return cfg, []

    if not models:
        raise ValueError("active=true ですが models が空です")

    ids = [m["id"] for m in models]
    if len(ids) != len(set(ids)):
        raise ValueError("model id が重複しています")

    return cfg, models


# =========================================================
# 基礎4戦略
# =========================================================

def basic_quality(r):
    return (
        num(r.get("spread_pct"), 999) <= MAX_SPREAD_PCT and
        num(r.get("amount24")) >= MIN_AMOUNT24
    )


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
    near = dist <= 0.35

    long_ok = (
        e9_15 > e21_15 and m15 > ms15 and
        e9_5 > e21_5 and near and
        38 <= rsi5 <= 58 and 35 <= rsi15 <= 70 and
        price >= e9_1 and m1 > ms1 and rsi1 >= 45
    )

    short_ok = (
        e9_15 < e21_15 and m15 < ms15 and
        e9_5 < e21_5 and near and
        42 <= rsi5 <= 62 and 30 <= rsi15 <= 65 and
        price <= e9_1 and m1 < ms1 and rsi1 <= 55
    )

    if long_ok:
        return "PB_LONG"
    if short_ok:
        return "PB_SHORT"
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

    if (
        price > e9_5 > e21_5 and e9_15 > e21_15 and
        m5 > ms5 and m15 > ms15 and
        v5 >= 1.50 and v1 >= 1.00 and edge >= 12
    ):
        return "TV_LONG"

    if (
        price < e9_5 < e21_5 and e9_15 < e21_15 and
        m5 < ms5 and m15 < ms15 and
        v5 >= 1.50 and v1 >= 1.00 and edge >= 12
    ):
        return "TV_SHORT"

    return None


def high_edge_signal(r):
    if not basic_quality(r):
        return None

    ls, ss = num(r.get("long_score")), num(r.get("short_score"))
    edge = abs(ls - ss)
    if edge < 35:
        return None

    direction = "LONG" if ls > ss else "SHORT"
    rsi15 = num(r.get("rsi15"))

    if direction == "LONG" and rsi15 >= 82:
        return None
    if direction == "SHORT" and rsi15 <= 18:
        return None

    return "HE_LONG" if direction == "LONG" else "HE_SHORT"


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

    if (
        price > e9_5 and e9_15 > e21_15 and
        m5 > ms5 and funding <= 0.00030
    ):
        return "OI_LONG"

    if (
        price < e9_5 and e9_15 < e21_15 and
        m5 < ms5 and funding >= -0.00030
    ):
        return "OI_SHORT"

    return None


def active_features(r):
    out = []
    for fn in (pullback_signal, trend_volume_signal, high_edge_signal, oi_funding_signal):
        v = fn(r)
        if v:
            out.append(v)
    return out


def feature_side(name):
    return "LONG" if name.endswith("_LONG") else "SHORT"


# =========================================================
# モデル評価
# =========================================================

def evaluate_model(model, features):
    long_score = 0.0
    short_score = 0.0
    reasons = []

    weights = model.get("weights", {})
    multipliers = model.get("primary_multipliers", {})

    for feat in features:
        w = float(weights.get(feat, 0.0))
        mult = float(multipliers.get(feat, 1.0))
        value = w * mult

        if feature_side(feat) == "LONG":
            long_score += value
        else:
            short_score += value

        if value != 0:
            reasons.append(f"{feat}:{value:+.2f}")

    fset = set(features)
    for inter in model.get("interactions", []):
        need = set(inter.get("when", []))
        if need and need.issubset(fset):
            delta = float(inter.get("delta", 0.0))
            target = inter.get("target", "LONG")
            if target == "LONG":
                long_score += delta
            elif target == "SHORT":
                short_score += delta
            reasons.append(f"INT({'+'.join(sorted(need))})->{target}:{delta:+.2f}")

    if long_score > short_score:
        direction = "LONG"
        score = long_score
        other = short_score
    elif short_score > long_score:
        direction = "SHORT"
        score = short_score
        other = long_score
    else:
        return None

    threshold = float(model.get("trade_score", 3.0))
    min_margin = float(model.get("min_margin", 1.0))
    margin = score - other

    if score < threshold or margin < min_margin:
        return None

    return {
        "direction": direction,
        "score_long": long_score,
        "score_short": short_score,
        "margin": margin,
        "reason": " / ".join(reasons)
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


# =========================================================
# 仮想ポジション管理
# =========================================================

def empty_state():
    return {
        "last_scan": None,
        "positions": {},
        "stats": {}
    }


def model_stats(state, model_id):
    s = state["stats"].setdefault(model_id, {
        "wins": 0, "losses": 0, "unknown": 0,
        "closed": 0, "total_r": 0.0,
        "max_equity_r": 0.0, "equity_r": 0.0,
        "max_drawdown_r": 0.0
    })
    return s


def open_count_for_model(state, model_id):
    prefix = model_id + "|"
    return sum(1 for k in state["positions"] if k.startswith(prefix))


def update_positions(state, rows_by_symbol):
    for key in list(state["positions"].keys()):
        p = state["positions"][key]
        r = rows_by_symbol.get(p["symbol"])
        if not r:
            continue

        current = num(r.get("price"))
        high5 = num(r.get("last_high5"), current)
        low5 = num(r.get("last_low5"), current)

        risk = abs(p["entry"] - p["sl"])
        if risk <= 0:
            continue

        if p["direction"] == "LONG":
            favorable = (high5 - p["entry"]) / risk
            adverse = (p["entry"] - low5) / risk
            hit_tp = high5 >= p["tp"]
            hit_sl = low5 <= p["sl"]
        else:
            favorable = (p["entry"] - low5) / risk
            adverse = (high5 - p["entry"]) / risk
            hit_tp = low5 <= p["tp"]
            hit_sl = high5 >= p["sl"]

        p["mfe_r"] = max(p.get("mfe_r", 0.0), favorable)
        p["mae_r"] = max(p.get("mae_r", 0.0), adverse)

        event = None
        result_r = None
        exit_price = ""

        if hit_tp and hit_sl:
            event = "UNKNOWN"
        elif hit_tp:
            event = "WIN"
            result_r = RR
            exit_price = p["tp"]
        elif hit_sl:
            event = "LOSS"
            result_r = -1.0
            exit_price = p["sl"]

        if not event:
            continue

        s = model_stats(state, p["model_id"])
        if event == "WIN":
            s["wins"] += 1
            s["closed"] += 1
            s["total_r"] += RR
            s["equity_r"] += RR
        elif event == "LOSS":
            s["losses"] += 1
            s["closed"] += 1
            s["total_r"] -= 1.0
            s["equity_r"] -= 1.0
        else:
            s["unknown"] += 1

        s["max_equity_r"] = max(s["max_equity_r"], s["equity_r"])
        dd = s["max_equity_r"] - s["equity_r"]
        s["max_drawdown_r"] = max(s["max_drawdown_r"], dd)

        append_trade({
            "time": now_iso(),
            "model_id": p["model_id"],
            "family": p["family"],
            "symbol": p["symbol"],
            "event": event,
            "direction": p["direction"],
            "entry": p["entry"],
            "sl": p["sl"],
            "tp": p["tp"],
            "exit": exit_price,
            "result_r": "" if result_r is None else result_r,
            "score_long": p["score_long"],
            "score_short": p["score_short"],
            "margin": p["margin"],
            "reason": p["reason"],
            "mfe_r": round(p["mfe_r"], 4),
            "mae_r": round(p["mae_r"], 4)
        })

        del state["positions"][key]


def open_best_for_each_model(state, models, rows, features_by_symbol):
    # モデルごとに、そのスキャン内でスコア余力が大きい候補から空き枠へ入れる
    for model in models:
        model_id = model["id"]
        family = model.get("family", "UNKNOWN")
        slots = MAX_OPEN_PER_MODEL - open_count_for_model(state, model_id)
        if slots <= 0:
            continue

        candidates = []
        for r in rows:
            symbol = r["symbol"]
            key = f"{model_id}|{symbol}"
            if key in state["positions"]:
                continue

            ev = evaluate_model(model, features_by_symbol[symbol])
            if not ev:
                continue

            candidates.append((ev["margin"], ev, r))

        candidates.sort(key=lambda x: x[0], reverse=True)

        for _, ev, r in candidates[:slots]:
            symbol = r["symbol"]
            entry, sl, tp = make_plan(r, ev["direction"])
            key = f"{model_id}|{symbol}"

            p = {
                "model_id": model_id,
                "family": family,
                "symbol": symbol,
                "direction": ev["direction"],
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "score_long": ev["score_long"],
                "score_short": ev["score_short"],
                "margin": ev["margin"],
                "reason": ev["reason"],
                "mfe_r": 0.0,
                "mae_r": 0.0,
                "opened_at": now_iso()
            }
            state["positions"][key] = p

            append_trade({
                "time": now_iso(),
                "model_id": model_id,
                "family": family,
                "symbol": symbol,
                "event": "OPEN",
                "direction": ev["direction"],
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "score_long": ev["score_long"],
                "score_short": ev["score_short"],
                "margin": ev["margin"],
                "reason": ev["reason"],
                "mfe_r": 0,
                "mae_r": 0
            })


# =========================================================
# 集計 / Discord BEST
# =========================================================

def write_summary(state, models):
    fields = [
        "model_id","family","closed","wins","losses","winrate",
        "total_r","expectancy_r","max_drawdown_r","open_positions"
    ]

    with open(SUMMARY_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()

        for m in models:
            mid = m["id"]
            s = model_stats(state, mid)
            closed = s["closed"]
            winrate = (s["wins"] / closed * 100) if closed else 0.0
            expectancy = (s["total_r"] / closed) if closed else 0.0

            w.writerow({
                "model_id": mid,
                "family": m.get("family",""),
                "closed": closed,
                "wins": s["wins"],
                "losses": s["losses"],
                "winrate": round(winrate, 4),
                "total_r": round(s["total_r"], 4),
                "expectancy_r": round(expectancy, 6),
                "max_drawdown_r": round(s["max_drawdown_r"], 4),
                "open_positions": open_count_for_model(state, mid)
            })


def choose_consensus_best(models, rows, features_by_symbol, state, cfg):
    # 511モデルの投票を7系統ごとにも集約し、
    # 単純モデル数多数決に偏りすぎないBEST候補を作る。
    min_closed_for_perf_weight = int(cfg.get("best_selector", {}).get("min_closed_for_perf_weight", 50))
    min_family_support = int(cfg.get("best_selector", {}).get("min_family_support", 4))
    min_total_support_pct = float(cfg.get("best_selector", {}).get("min_total_support_pct", 55.0))

    model_map = {m["id"]: m for m in models}
    best = None

    for r in rows:
        symbol = r["symbol"]
        votes = []
        family_votes = {}

        for m in models:
            ev = evaluate_model(m, features_by_symbol[symbol])
            if not ev:
                continue

            s = model_stats(state, m["id"])
            perf_weight = 1.0
            if s["closed"] >= min_closed_for_perf_weight:
                # 極端な重みを避ける。期待値が良いモデルを少しだけ優遇。
                exp_r = s["total_r"] / s["closed"]
                perf_weight = max(0.5, min(1.5, 1.0 + exp_r))

            family = m.get("family", "UNKNOWN")
            family_votes.setdefault(family, {"LONG": 0.0, "SHORT": 0.0})
            family_votes[family][ev["direction"]] += perf_weight
            votes.append((ev["direction"], perf_weight, ev, m))

        if not votes:
            continue

        total_w = sum(v[1] for v in votes)
        long_w = sum(v[1] for v in votes if v[0] == "LONG")
        short_w = sum(v[1] for v in votes if v[0] == "SHORT")

        direction = "LONG" if long_w > short_w else "SHORT"
        support_w = max(long_w, short_w)
        support_pct = support_w / total_w * 100 if total_w else 0.0

        family_support = 0
        for fv in family_votes.values():
            if fv[direction] > fv["SHORT" if direction == "LONG" else "LONG"]:
                family_support += 1

        if support_pct < min_total_support_pct or family_support < min_family_support:
            continue

        # 支持率と系統数を主に評価。spreadも軽く考慮。
        spread_penalty = num(r.get("spread_pct")) * 10
        quality = support_pct + family_support * 5 - spread_penalty

        item = {
            "symbol": symbol,
            "direction": direction,
            "support_pct": support_pct,
            "family_support": family_support,
            "quality": quality,
            "price": num(r.get("price")),
            "atr5_pct": num(r.get("atr5_pct")),
            "long_weight": long_w,
            "short_weight": short_w
        }

        if best is None or item["quality"] > best["quality"]:
            best = item

    return best


def discord_send_best(best, cfg):
    if not best:
        return
    if not cfg.get("discord_enabled", False):
        return
    if not DISCORD_WEBHOOK_URL:
        print("BEST候補あり。ただしDISCORD_LAB_WEBHOOK_URLなし")
        return

    try:
        import requests
    except Exception:
        return

    price = best["price"]
    # 表示用TP/SL
    fake_row = {"price": price, "atr5_pct": best["atr5_pct"]}
    _, sl, tp = make_plan(fake_row, best["direction"])

    text = (
        f"🏆 **LAB BEST {best['direction']} | {best['symbol']}**\n"
        f"価格: **{price:.8g}**\n"
        f"TP: {tp:.8g} / SL: {sl:.8g}\n"
        f"支持率: **{best['support_pct']:.1f}%**\n"
        f"支持系統: **{best['family_support']}/7**\n"
        f"Weighted LONG {best['long_weight']:.1f} / SHORT {best['short_weight']:.1f}"
    )

    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=15)
    except Exception as e:
        print("Discord error:", e)


# =========================================================
# Main
# =========================================================

def main():
    t0 = time.perf_counter()

    cfg, models = load_config()
    if not models:
        return

    t1 = time.perf_counter()

    scan = latest_scan()
    scan_name = os.path.basename(scan)
    with open(scan, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    t2 = time.perf_counter()

    state = load_json(STATE_FILE, empty_state())
    if state.get("last_scan") == scan_name:
        print("同じCSVは処理済み:", scan_name)
        return

    features_by_symbol = {
        r["symbol"]: active_features(r)
        for r in rows
    }

    t3 = time.perf_counter()

    rows_by_symbol = {r["symbol"]: r for r in rows}
    update_positions(state, rows_by_symbol)
    open_best_for_each_model(state, models, rows, features_by_symbol)

    t4 = time.perf_counter()

    write_summary(state, models)
    best = choose_consensus_best(models, rows, features_by_symbol, state, cfg)
    discord_send_best(best, cfg)

    state["last_scan"] = scan_name
    save_json(STATE_FILE, state)

    t5 = time.perf_counter()

    print("HYBRID LAB 完了")
    print("models:", len(models))
    print("open positions:", len(state["positions"]))
    if best:
        print("BEST:", best["symbol"], best["direction"], round(best["support_pct"],2), "%")
    else:
        print("BEST: 条件未達")

    print(
        "TIME sec | "
        f"config {t1-t0:.3f} | "
        f"csv {t2-t1:.3f} | "
        f"features {t3-t2:.3f} | "
        f"models+positions {t4-t3:.3f} | "
        f"save+best {t5-t4:.3f} | "
        f"total {t5-t0:.3f}"
    )


if __name__ == "__main__":
    main()
