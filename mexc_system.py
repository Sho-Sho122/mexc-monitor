import csv
import glob
import json
import os
import time
from datetime import datetime, timedelta
import requests

# =========================================================
# MEXC FREE SIGNAL ENGINE + DISCORD + PAPER TRADING
# GPT/API課金なし。最新の mexc_scan_*.csv を読み込んで判定。
# =========================================================

BASE_BALANCE = 38.0
LEVERAGE = 5
RISK_PER_TRADE = 0.015          # 1回の最大想定損失: 仮想残高の1.5%
MAX_TOTAL_OPEN_RISK = 0.035     # 全保有合計: 仮想残高の3.5%
MAX_MARGIN_PER_TRADE = 20.0
MAX_OPEN_POSITIONS = 2

# A / B+ / B
A_EDGE = 26.0
BPLUS_EDGE = 22.0
B_EDGE = 18.0

# データ安全装置
MAX_TICKER_AGE_SEC = 180        # 3分超は新規A/B+を抑制
MAX_SCAN_AGE_SEC = 300
MAX_SPREAD_PCT = 0.05
MIN_AMOUNT24 = 5_000_000        # 24h USDT出来高の最低目安
MAX_ABS_CHANGE24 = 0.35         # 35%以上の24h変動は新規A抑制
MIN_A_VOLUME_RATIO = 1.20

# Discord
WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"
NOTIFY_NORMAL_B = True
HEARTBEAT_HOURS = 1

STATE_FILE = "mexc_system_state.json"
TRADE_LOG = "mexc_paper_trades.csv"

CRYPTO_MAJOR = {"BTC_USDT", "ETH_USDT"}
CRYPTO_ALTS = {
    "XRP_USDT","SOL_USDT","DOGE_USDT","LINK_USDT","PEPE_USDT","AAVE_USDT",
    "ENA_USDT","SUI_USDT","PUMPFUN_USDT","TAO_USDT","WLD_USDT","ADA_USDT",
    "AVAX_USDT","TUT_USDT","ONDO_USDT","NEAR_USDT","XLM_USDT","HYPE_USDT","ZEC_USDT"
}

def num(x, default=0.0):
    try:
        if x in ("", None, "None", "nan", "NaN"):
            return default
        return float(x)
    except Exception:
        return default

def parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None

def now():
    return datetime.now()

def latest_scan():
    files = glob.glob("mexc_scan_*.csv")
    if not files:
        raise FileNotFoundError("mexc_scan_*.csv がありません")
    return max(files, key=os.path.getmtime)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "balance": BASE_BALANCE,
            "signals": {},
            "orders": {},
            "positions": {},
            "stats": {"wins":0, "losses":0, "unknown":0, "total_pnl":0.0},
            "last_heartbeat": None,
            "last_processed_scan": None
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        s.setdefault("balance", BASE_BALANCE)
        s.setdefault("signals", {})
        s.setdefault("orders", {})
        s.setdefault("positions", {})
        s.setdefault("stats", {"wins":0, "losses":0, "unknown":0, "total_pnl":0.0})
        s.setdefault("last_heartbeat", None)
        s.setdefault("last_processed_scan", None)
        return s
    except Exception:
        # 壊れたstateはバックアップして初期化
        try:
            os.replace(STATE_FILE, STATE_FILE + ".broken_" + now().strftime("%Y%m%d_%H%M%S"))
        except Exception:
            pass
        return load_state()

def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def discord_send(title, body, color=None):
    url = os.getenv(WEBHOOK_ENV)
    if not url:
        print("Discord未送信: DISCORD_WEBHOOK_URL がありません")
        return False
    payload = {"username": "MEXC Signal"}
    if color is None:
        payload["content"] = f"**{title}**\n{body}"[:1900]
    else:
        payload["embeds"] = [{
            "title": title[:256],
            "description": body[:4000],
            "color": color,
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }]
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code not in (200, 204):
            print("Discord失敗:", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        print("Discord例外:", repr(e))
        return False

def market_group(symbol):
    if symbol in CRYPTO_MAJOR:
        return "crypto_major"
    if symbol in CRYPTO_ALTS:
        return "crypto_alt"
    if "XAU" in symbol or "SILVER" in symbol:
        return "metal"
    if "STOCK" in symbol or symbol in {"SOXL_USDT","SPX500_USDT"}:
        return "equity"
    return "other"

def data_quality(r, scan_dt):
    problems = []
    ticker_dt = parse_dt(r.get("ticker_time_jst"))
    t = now()
    if ticker_dt is None:
        problems.append("ticker時刻なし")
    elif (t - ticker_dt).total_seconds() > MAX_TICKER_AGE_SEC:
        problems.append("ticker古い")
    if scan_dt is None:
        problems.append("scan時刻なし")
    elif (t - scan_dt).total_seconds() > MAX_SCAN_AGE_SEC:
        problems.append("scan古い")
    if num(r.get("price")) <= 0:
        problems.append("価格異常")
    if num(r.get("spread_pct"), 999) > MAX_SPREAD_PCT:
        problems.append("spread大")
    if num(r.get("amount24")) < MIN_AMOUNT24:
        problems.append("出来高不足")
    if abs(num(r.get("change24"))) > MAX_ABS_CHANGE24:
        problems.append("24h価格飛び")
    # OIが0は取得失敗の可能性。欠損は初回などあり得るため警告扱い。
    if num(r.get("oi")) <= 0:
        problems.append("OI異常")
    return problems

def aligned_long(r):
    return (
        num(r["price"]) > num(r["ema9_5"]) > num(r["ema21_5"]) and
        num(r["ema9_15"]) > num(r["ema21_15"]) and
        num(r["macd5"]) > num(r["macd_signal5"]) and
        num(r["macd15"]) > num(r["macd_signal15"])
    )

def aligned_short(r):
    return (
        num(r["price"]) < num(r["ema9_5"]) < num(r["ema21_5"]) and
        num(r["ema9_15"]) < num(r["ema21_15"]) and
        num(r["macd5"]) < num(r["macd_signal5"]) and
        num(r["macd15"]) < num(r["macd_signal15"])
    )

def classify(r, scan_dt):
    ls, ss = num(r["long_score"]), num(r["short_score"])
    direction = "LONG" if ls > ss else "SHORT"
    edge = abs(ls - ss)
    probs = data_quality(r, scan_dt)

    trend = aligned_long(r) if direction == "LONG" else aligned_short(r)
    rsi5, rsi15 = num(r["rsi5"]), num(r["rsi15"])
    rsi_ok = (rsi5 < 72 and rsi15 < 72) if direction == "LONG" else (rsi5 > 28 and rsi15 > 25)
    volume_ok = max(num(r["volume_ratio1"]), num(r["volume_ratio5"])) >= MIN_A_VOLUME_RATIO

    # 安全装置に引っかかる銘柄はA/B+禁止。通常BまたはCへ。
    safe = len(probs) == 0

    if safe and edge >= A_EDGE and trend and rsi_ok and volume_ok:
        grade = "A"
    elif safe and edge >= BPLUS_EDGE and trend and rsi_ok:
        # Aまで「出来高」またはedge等が少し足りない状態
        grade = "B+"
    elif edge >= B_EDGE and num(r.get("spread_pct"),999) <= MAX_SPREAD_PCT:
        grade = "B"
    else:
        grade = "C"

    missing = []
    if edge < A_EDGE: missing.append(f"edge {edge:.1f}/{A_EDGE:.0f}")
    if not trend: missing.append("5m/15mトレンド整合")
    if not rsi_ok: missing.append("RSI条件")
    if not volume_ok: missing.append(f"出来高比<{MIN_A_VOLUME_RATIO}")
    if probs: missing.append("DATA:" + ",".join(probs))

    return grade, direction, edge, probs, missing

def plan(r, grade, direction, balance):
    price = num(r["price"])
    ema9 = num(r["ema9_5"], price)
    atr5 = max(num(r["atr5_pct"]), 0.10) / 100.0
    high5, low5 = num(r["recent_high5"]), num(r["recent_low5"])
    stop_pct = max(atr5 * 1.20, 0.0035)

    entry = price if grade == "A" else ema9
    if direction == "LONG":
        atr_sl = entry * (1-stop_pct)
        structure = low5 * 0.998 if low5 else atr_sl
        sl = min(atr_sl, structure)
        risk_pct = max((entry-sl)/entry, .001)
        tp = entry * (1 + risk_pct*2)
    else:
        atr_sl = entry * (1+stop_pct)
        structure = high5 * 1.002 if high5 else atr_sl
        sl = max(atr_sl, structure)
        risk_pct = max((sl-entry)/entry, .001)
        tp = entry * (1 - risk_pct*2)

    risk_budget = balance * RISK_PER_TRADE
    notional = min(risk_budget/risk_pct, MAX_MARGIN_PER_TRADE*LEVERAGE)
    margin = notional/LEVERAGE
    qty = notional/entry
    loss = notional*risk_pct
    profit = loss*2
    valid_pct = min(max(num(r["atr1_pct"])*0.6, .10), .35)
    return dict(entry=entry, sl=sl, tp=tp, rr=2.0, risk_pct=risk_pct,
                notional=notional, margin=margin, qty=qty,
                loss=loss, profit=profit, valid_pct=valid_pct)

def can_add_position(state, symbol, direction, planned_loss):
    positions = state["positions"]
    if symbol in positions:
        return False, "同銘柄の仮想ポジション保有中"
    if len(positions) >= MAX_OPEN_POSITIONS:
        return False, "最大同時ポジション数"
    total_risk = sum(num(p.get("planned_loss")) for p in positions.values())
    if total_risk + planned_loss > state["balance"] * MAX_TOTAL_OPEN_RISK:
        return False, "合計リスク上限"

    group = market_group(symbol)
    # 同方向の暗号資産を重ねすぎない
    for s, p in positions.items():
        if p.get("direction") == direction:
            g2 = market_group(s)
            if group.startswith("crypto") and g2.startswith("crypto"):
                return False, "暗号資産の同方向相関リスク"
    return True, ""

def append_trade_log(row):
    exists = os.path.exists(TRADE_LOG)
    fields = ["time","symbol","event","direction","entry","sl","tp","exit","pnl","balance","note"]
    with open(TRADE_LOG, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k,"") for k in fields})

def open_position(state, symbol, direction, p, source):
    state["positions"][symbol] = {
        "direction": direction, "entry": p["entry"], "sl": p["sl"], "tp": p["tp"],
        "qty": p["qty"], "notional": p["notional"], "planned_loss": p["loss"],
        "opened_at": now().isoformat(), "source": source
    }
    append_trade_log({
        "time": now().isoformat(), "symbol": symbol, "event":"OPEN",
        "direction":direction, "entry":p["entry"], "sl":p["sl"], "tp":p["tp"],
        "balance":state["balance"], "note":source
    })

def close_position(state, symbol, exit_price, result, note=""):
    p = state["positions"].pop(symbol)
    if p["direction"] == "LONG":
        pnl = (exit_price - p["entry"]) * p["qty"]
    else:
        pnl = (p["entry"] - exit_price) * p["qty"]
    state["balance"] += pnl
    state["stats"]["total_pnl"] += pnl
    if result == "WIN": state["stats"]["wins"] += 1
    elif result == "LOSS": state["stats"]["losses"] += 1
    else: state["stats"]["unknown"] += 1
    append_trade_log({
        "time":now().isoformat(),"symbol":symbol,"event":result,
        "direction":p["direction"],"entry":p["entry"],"sl":p["sl"],"tp":p["tp"],
        "exit":exit_price,"pnl":pnl,"balance":state["balance"],"note":note
    })
    return pnl, p

def update_existing_positions(state, rows_by_symbol):
    messages = []
    for symbol in list(state["positions"].keys()):
        if symbol not in rows_by_symbol:
            continue
        r = rows_by_symbol[symbol]
        p = state["positions"][symbol]
        # 直近5分のhigh/lowで判定。両方到達なら順序不明なので保守的にUNKNOWN。
        hi = max(num(r.get("recent_high5")), num(r.get("price")))
        lo = min(num(r.get("recent_low5")), num(r.get("price")))
        if p["direction"] == "LONG":
            hit_tp, hit_sl = hi >= p["tp"], lo <= p["sl"]
        else:
            hit_tp, hit_sl = lo <= p["tp"], hi >= p["sl"]

        if hit_tp and hit_sl:
            # 勝ち扱いしない。仮想残高は変更せず、ポジションを判定不能で閉じる。
            old = state["positions"].pop(symbol)
            state["stats"]["unknown"] += 1
            append_trade_log({
                "time":now().isoformat(),"symbol":symbol,"event":"UNKNOWN",
                "direction":old["direction"],"entry":old["entry"],"sl":old["sl"],"tp":old["tp"],
                "balance":state["balance"],"note":"同一5分範囲でTP/SL双方到達・順序不明"
            })
            messages.append(f"⚪ {symbol} 判定不能：同一5分範囲でTP/SL双方到達")
        elif hit_tp:
            pnl, old = close_position(state, symbol, p["tp"], "WIN", "TP到達")
            messages.append(f"✅ {symbol} TP +{pnl:.2f} USDT")
        elif hit_sl:
            pnl, old = close_position(state, symbol, p["sl"], "LOSS", "SL到達")
            messages.append(f"❌ {symbol} SL {pnl:.2f} USDT")
    return messages

def update_orders(state, rows_by_symbol, current_results):
    messages = []
    for symbol in list(state["orders"].keys()):
        o = state["orders"][symbol]
        r = rows_by_symbol.get(symbol)
        res = current_results.get(symbol)
        if not r or not res:
            continue
        grade, direction = res["grade"], res["direction"]

        # 方向が反転、C化、またはデータ異常ならキャンセル
        if direction != o["direction"] or grade == "C" or res["problems"]:
            state["orders"].pop(symbol)
            messages.append(f"🗑️ {symbol} 指値キャンセル ({o['direction']} → {grade} {direction})")
            continue

        hi = max(num(r.get("recent_high5")), num(r.get("price")))
        lo = min(num(r.get("recent_low5")), num(r.get("price")))
        hit = lo <= o["entry"] <= hi
        if hit:
            ok, why = can_add_position(state, symbol, o["direction"], o["planned_loss"])
            if ok:
                p = dict(entry=o["entry"], sl=o["sl"], tp=o["tp"], qty=o["qty"],
                         notional=o["notional"], loss=o["planned_loss"])
                open_position(state, symbol, o["direction"], p, "LIMIT")
                state["orders"].pop(symbol)
                messages.append(f"🎯 {symbol} {o['direction']} 指値約定 @ {o['entry']:.10g}")
            else:
                state["orders"].pop(symbol)
                messages.append(f"🗑️ {symbol} 指値到達もリスク制限で見送り: {why}")
    return messages

def signal_body(r, res, p, transition):
    missing = " / ".join(res["missing"]) if res["missing"] else "A条件充足"
    return (
        f"**{r['symbol']} | {res['direction']} | {transition}**\n"
        f"現在値 `{num(r['price']):.10g}`  取得 `{r.get('ticker_time_jst','')}`\n"
        f"Entry `{p['entry']:.10g}` / SL `{p['sl']:.10g}` / TP `{p['tp']:.10g}` / RR `1:{p['rr']:.1f}`\n"
        f"分離 {LEVERAGE}x / 建玉 `{p['notional']:.2f}` USDT / 証拠金 `{p['margin']:.2f}` USDT\n"
        f"SL概算 `{p['loss']:.2f}` / TP概算 `{p['profit']:.2f}` USDT\n"
        f"RSI 1m/5m/15m `{num(r['rsi1']):.1f}/{num(r['rsi5']):.1f}/{num(r['rsi15']):.1f}`\n"
        f"出来高比 1m/5m `{num(r['volume_ratio1']):.2f}/{num(r['volume_ratio5']):.2f}` / edge `{res['edge']:.1f}`\n"
        f"Aまで: {missing}\n"
        f"有効範囲: 取得価格から±`{p['valid_pct']:.2f}%`。超えたら再評価。"
    )

def main():
    state = load_state()
    scan_path = latest_scan()

    # 同じCSVを二重処理しない
    scan_id = os.path.basename(scan_path)
    if state.get("last_processed_scan") == scan_id:
        print("同じCSVは処理済み:", scan_id)
        return

    with open(scan_path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise RuntimeError("CSVが空です")

    scan_dt = parse_dt(rows[0].get("scan_time_jst"))
    rows_by_symbol = {r["symbol"]:r for r in rows}

    results = {}
    for r in rows:
        grade, direction, edge, probs, missing = classify(r, scan_dt)
        results[r["symbol"]] = {
            "grade":grade, "direction":direction, "edge":edge,
            "problems":probs, "missing":missing
        }

    # 既存仮想ポジション/指値を先に更新
    event_msgs = update_existing_positions(state, rows_by_symbol)
    event_msgs += update_orders(state, rows_by_symbol, results)
    if event_msgs:
        discord_send("📒 仮想取引更新", "\n".join(event_msgs), 0x3498DB)

    prev_signals = state.get("signals", {})
    new_signals = {}

    # edge順
    ranked = sorted(rows, key=lambda r: results[r["symbol"]]["edge"], reverse=True)

    for r in ranked:
        symbol = r["symbol"]
        res = results[symbol]
        grade, direction = res["grade"], res["direction"]
        prev = prev_signals.get(symbol, {"grade":"C","direction":direction})
        prev_grade, prev_dir = prev.get("grade","C"), prev.get("direction",direction)
        transition = f"{prev_grade}→{grade}"
        p = plan(r, grade, direction, state["balance"])

        # 通知ルール
        notify_detail = False
        notify_short = False
        invalidation = False

        if prev_grade == "A" and grade == "C":
            invalidation = True
        elif prev_grade == "A" and prev_dir != direction:
            notify_detail = grade in ("A","B+")
            if not notify_detail: notify_short = True
        elif prev_grade == "B" and grade == "B+":
            notify_detail = True
        elif prev_grade == "B+" and grade == "A":
            notify_detail = True
        elif grade == "A" and prev_grade != "A":
            notify_detail = True
        elif grade == "B+" and prev_grade not in ("B+","A"):
            notify_detail = True
        elif grade == "B" and prev_grade != "B" and NOTIFY_NORMAL_B:
            notify_short = True
        # A→A, B→B, B+→B+ は原則通知なし

        if invalidation:
            discord_send("⚪ A失効", f"{symbol} | {prev_dir} | A→C\n現在値 `{num(r['price']):.10g}`", 0x95A5A6)
        elif notify_detail:
            color = 0xE74C3C if grade == "A" else 0xF39C12
            title = "🔴 A SIGNAL" if grade == "A" else "🟠 B+ A候補接近"
            discord_send(title, signal_body(r,res,p,transition), color)
        elif notify_short:
            action = "現在値付近" if grade == "A" else f"{p['entry']:.10g} 指値"
            discord_send("B SIGNAL", f"{symbol} {direction} | {action} | edge {res['edge']:.1f}")

        # 仮想注文管理
        if grade == "A" and symbol not in state["positions"]:
            # Aは取得価格で即時仮想約定。ただしリスク制御。
            ok, why = can_add_position(state, symbol, direction, p["loss"])
            if ok:
                # B指値があれば取り消してAで即時
                state["orders"].pop(symbol, None)
                open_position(state, symbol, direction, p, "A_MARKET")
            else:
                # 通知は必要な変化時だけ
                if notify_detail:
                    discord_send("⚠️ A見送り", f"{symbol} {direction}: {why}", 0x7F8C8D)

        elif grade in ("B+","B") and symbol not in state["positions"]:
            # B/B+は指値。毎回最新プランへ更新。
            state["orders"][symbol] = {
                "grade":grade, "direction":direction, "entry":p["entry"], "sl":p["sl"], "tp":p["tp"],
                "qty":p["qty"], "notional":p["notional"], "planned_loss":p["loss"],
                "updated_at":now().isoformat()
            }
        elif grade == "C":
            state["orders"].pop(symbol, None)

        new_signals[symbol] = {
            "grade":grade, "direction":direction, "edge":res["edge"],
            "price":num(r["price"]), "time":now().isoformat()
        }

    state["signals"] = new_signals
    state["last_processed_scan"] = scan_id

    # 成績
    st = state["stats"]
    decided = st["wins"] + st["losses"]
    winrate = st["wins"]/decided*100 if decided else 0
    roi = (state["balance"]/BASE_BALANCE-1)*100

    # 1時間ごとの正常稼働通知
    hb = parse_dt(state.get("last_heartbeat"))
    if hb is None or now()-hb >= timedelta(hours=HEARTBEAT_HOURS):
        discord_send(
            "🟢 Scanner正常稼働",
            f"CSV `{scan_id}`\n"
            f"仮想残高 `{state['balance']:.2f}` USDT / 損益 `{st['total_pnl']:+.2f}`\n"
            f"勝敗 `{st['wins']}勝 {st['losses']}敗` / 判定不能 `{st['unknown']}` / 勝率 `{winrate:.1f}%`\n"
            f"回収率 `{roi:+.1f}%` / 保有 `{len(state['positions'])}` / 指値 `{len(state['orders'])}`",
            0x2ECC71
        )
        state["last_heartbeat"] = now().strftime("%Y-%m-%d %H:%M:%S.%f")

    save_state(state)

    # ローカルサマリー
    summary_name = now().strftime("signal_%Y%m%d_%H%M%S.txt")
    counts = {g:sum(1 for x in results.values() if x["grade"]==g) for g in ("A","B+","B","C")}
    with open(summary_name, "w", encoding="utf-8") as f:
        f.write("今回やること\n")
        f.write(f"使用CSV: {scan_id}\n")
        f.write(f"A {counts['A']} / B+ {counts['B+']} / B {counts['B']} / C {counts['C']}\n")
        f.write(f"仮想残高: {state['balance']:.2f} USDT\n")
        f.write(f"累計: {st['wins']}勝 {st['losses']}敗 判定不能{st['unknown']} 総損益 {st['total_pnl']:+.2f} USDT 回収率 {roi:+.1f}%\n\n")
        for r in ranked:
            res=results[r["symbol"]]
            if res["grade"] in ("A","B+","B"):
                f.write(f"{res['grade']} | {r['symbol']} | {res['direction']} | price {num(r['price']):.10g} | edge {res['edge']:.1f}\n")
        # 必ずCを最低1つ記載
        cs=[r for r in ranked if results[r["symbol"]]["grade"]=="C"]
        if cs:
            r=cs[0]; res=results[r["symbol"]]
            f.write(f"C | {r['symbol']} | {res['direction']} | price {num(r['price']):.10g} | edge {res['edge']:.1f}\n")

    print("完了:", scan_id)
    print(f"A={counts['A']} B+={counts['B+']} B={counts['B']} C={counts['C']}")
    print(f"仮想残高={state['balance']:.2f} USDT")
    print("保存:", summary_name)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("SYSTEM ERROR:", repr(e))
        discord_send("⚠️ MEXC Scanner ERROR", f"`{type(e).__name__}: {str(e)[:1500]}`", 0xE74C3C)
        raise
