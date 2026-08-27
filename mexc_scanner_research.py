import requests
import time
import csv
import json
import os
from datetime import datetime, timezone, timedelta

BASE_URL = "https://api.mexc.com"
JST = timezone(timedelta(hours=9))

DETAIL_COUNT = 30
TOP_COUNT = 10
MIN_AMOUNT24 = 1_000_000
MAX_SPREAD_PCT = 0.20
API_SLEEP = 0.12
OI_STATE_FILE = "mexc_oi_state_research.json"


def get_json(url, params=None):
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"MEXC API ERROR: {data}")

    return data


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def ms_to_jst(timestamp_ms):
    try:
        timestamp_ms = int(timestamp_ms)
        dt = datetime.fromtimestamp(
            timestamp_ms / 1000,
            tz=timezone.utc
        ).astimezone(JST)
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except:
        return ""


def ema_series(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)
    first = sum(values[:period]) / period

    result = [None] * (period - 1)
    result.append(first)

    previous = first

    for price in values[period:]:
        current = price * multiplier + previous * (1 - multiplier)
        result.append(current)
        previous = current

    return result


def last_ema(values, period):
    series = ema_series(values, period)
    if not series:
        return None
    return series[-1]


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(values):
    if len(values) < 35:
        return None, None, None

    ema12 = ema_series(values, 12)
    ema26 = ema_series(values, 26)

    macd_values = []

    for i in range(len(values)):
        e12 = ema12[i] if i < len(ema12) else None
        e26 = ema26[i] if i < len(ema26) else None

        if e12 is not None and e26 is not None:
            macd_values.append(e12 - e26)

    if len(macd_values) < 9:
        return None, None, None

    signal_series = ema_series(macd_values, 9)

    macd_line = macd_values[-1]
    signal = signal_series[-1]

    if signal is None:
        return macd_line, None, None

    histogram = macd_line - signal
    return macd_line, signal, histogram


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        true_ranges.append(tr)

    return sum(true_ranges[-period:]) / period


def get_klines(symbol, interval, bars=120):
    seconds = {
        "Min1": 60,
        "Min5": 300,
        "Min15": 900,
        "Min60": 3600
    }

    end = int(time.time())
    start = end - seconds[interval] * bars

    url = f"{BASE_URL}/api/v1/contract/kline/{symbol}"

    response = get_json(
        url,
        {
            "interval": interval,
            "start": start,
            "end": end
        }
    )

    d = response.get("data", {})

    times = d.get("time", [])
    opens = [safe_float(x) for x in d.get("open", [])]
    closes = [safe_float(x) for x in d.get("close", [])]
    highs = [safe_float(x) for x in d.get("high", [])]
    lows = [safe_float(x) for x in d.get("low", [])]
    volumes = [safe_float(x) for x in d.get("vol", [])]

    # 形成途中の最新足は除外
    if len(closes) >= 2:
        times = times[:-1]
        opens = opens[:-1]
        closes = closes[:-1]
        highs = highs[:-1]
        lows = lows[:-1]
        volumes = volumes[:-1]

    return {
        "time": times,
        "open": opens,
        "close": closes,
        "high": highs,
        "low": lows,
        "volume": volumes
    }


def analyse_timeframe(k):
    closes = k["close"]
    highs = k["high"]
    lows = k["low"]
    volumes = k["volume"]

    if len(closes) < 40:
        return None

    current = closes[-1]

    e9 = last_ema(closes, 9)
    e21 = last_ema(closes, 21)

    current_rsi = rsi(closes, 14)

    macd_line, signal, hist = macd(closes)

    current_atr = atr(highs, lows, closes, 14)

    latest_volume = volumes[-1]
    previous_volumes = volumes[-21:-1]

    avg_volume = (
        sum(previous_volumes) / len(previous_volumes)
        if previous_volumes else 0
    )

    volume_ratio = (
        latest_volume / avg_volume
        if avg_volume > 0 else 0
    )

    long_score = 0
    short_score = 0

    if e9 is not None and e21 is not None:
        if current > e9 > e21:
            long_score += 3
        elif current < e9 < e21:
            short_score += 3
        elif e9 > e21:
            long_score += 1
        elif e9 < e21:
            short_score += 1

    if current_rsi is not None:
        if 52 <= current_rsi <= 68:
            long_score += 2
        elif 32 <= current_rsi <= 48:
            short_score += 2

        if current_rsi >= 78:
            short_score += 1
        elif current_rsi <= 22:
            long_score += 1

    if macd_line is not None and signal is not None:
        if macd_line > signal and macd_line > 0:
            long_score += 2
        elif macd_line < signal and macd_line < 0:
            short_score += 2
        elif macd_line > signal:
            long_score += 1
        elif macd_line < signal:
            short_score += 1

    if closes[-1] > closes[-2]:
        long_score += 1
    elif closes[-1] < closes[-2]:
        short_score += 1

    if volume_ratio >= 1.5:
        if closes[-1] > closes[-2]:
            long_score += 2
        elif closes[-1] < closes[-2]:
            short_score += 2

    recent_high = max(highs[-20:])
    recent_low = min(lows[-20:])

    return {
        "close": current,
        "ema9": e9,
        "ema21": e21,
        "rsi": current_rsi,
        "macd": macd_line,
        "macd_signal": signal,
        "macd_hist": hist,
        "atr": current_atr,
        "volume_ratio": volume_ratio,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "last_high": highs[-1],
        "last_low": lows[-1],
        "long": long_score,
        "short": short_score
    }



def pct_return(closes, lookback=1):
    if len(closes) <= lookback:
        return 0.0
    old = closes[-1 - lookback]
    new = closes[-1]
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100


def get_btc_market_snapshot():
    """
    BTC_USDTの市場状態を1スキャンにつき1回だけ取得する。
    完成済みレジーム名はまだ決めず、生の特徴量を保存する。
    """
    symbol = "BTC_USDT"

    k1 = get_klines(symbol, "Min1", 120)
    time.sleep(API_SLEEP)

    k5 = get_klines(symbol, "Min5", 120)
    time.sleep(API_SLEEP)

    k15 = get_klines(symbol, "Min15", 120)
    time.sleep(API_SLEEP)

    k60 = get_klines(symbol, "Min60", 120)
    time.sleep(API_SLEEP)

    a1 = analyse_timeframe(k1)
    a5 = analyse_timeframe(k5)
    a15 = analyse_timeframe(k15)
    a60 = analyse_timeframe(k60)

    if a1 is None or a5 is None or a15 is None or a60 is None:
        raise RuntimeError("BTC市場スナップショットのKline不足")

    btc_price = a1["close"]

    def atr_pct(a):
        return (a["atr"] / btc_price * 100) if a["atr"] else 0.0

    return {
        "btc_price": btc_price,

        "btc_ret_1m_pct": pct_return(k1["close"], 1),
        "btc_ret_5m_pct": pct_return(k5["close"], 1),
        "btc_ret_15m_pct": pct_return(k15["close"], 1),
        "btc_ret_1h_pct": pct_return(k60["close"], 1),

        "btc_rsi1": a1["rsi"],
        "btc_rsi5": a5["rsi"],
        "btc_rsi15": a15["rsi"],
        "btc_rsi60": a60["rsi"],

        "btc_ema9_1": a1["ema9"],
        "btc_ema21_1": a1["ema21"],
        "btc_ema9_5": a5["ema9"],
        "btc_ema21_5": a5["ema21"],
        "btc_ema9_15": a15["ema9"],
        "btc_ema21_15": a15["ema21"],
        "btc_ema9_60": a60["ema9"],
        "btc_ema21_60": a60["ema21"],

        "btc_macd1": a1["macd"],
        "btc_macd_signal1": a1["macd_signal"],
        "btc_macd5": a5["macd"],
        "btc_macd_signal5": a5["macd_signal"],
        "btc_macd15": a15["macd"],
        "btc_macd_signal15": a15["macd_signal"],
        "btc_macd60": a60["macd"],
        "btc_macd_signal60": a60["macd_signal"],

        "btc_volume_ratio1": a1["volume_ratio"],
        "btc_volume_ratio5": a5["volume_ratio"],
        "btc_volume_ratio15": a15["volume_ratio"],
        "btc_volume_ratio60": a60["volume_ratio"],

        "btc_atr1_pct": atr_pct(a1),
        "btc_atr5_pct": atr_pct(a5),
        "btc_atr15_pct": atr_pct(a15),
        "btc_atr60_pct": atr_pct(a60),
    }

def load_previous_oi():
    if not os.path.exists(OI_STATE_FILE):
        return {}

    try:
        with open(OI_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def save_current_oi(data):
    with open(OI_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


previous_oi = load_previous_oi()

print()
print("=" * 110)
print("MEXC USDT無期限先物 完成版スキャナー")
print("=" * 110)

scan_start = datetime.now(JST)

print(
    "スキャン開始:",
    scan_start.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
    "JST"
)

ticker_response = get_json(
    f"{BASE_URL}/api/v1/contract/ticker"
)

tickers = ticker_response.get("data", [])

usdt = [
    x
    for x in tickers
    if str(x.get("symbol", "")).endswith("_USDT")
]

print("USDT無期限:", len(usdt), "銘柄")

current_oi_state = {}

for x in usdt:
    symbol = x.get("symbol")
    oi = safe_float(x.get("holdVol"))

    current_oi_state[symbol] = {
        "oi": oi,
        "timestamp": int(time.time())
    }


candidates = []

for x in usdt:
    symbol = x.get("symbol")

    price = safe_float(x.get("lastPrice"))
    bid = safe_float(x.get("bid1"))
    ask = safe_float(x.get("ask1"))
    volume24 = safe_float(x.get("volume24"))
    amount24 = safe_float(x.get("amount24"))
    change24 = safe_float(x.get("riseFallRate"))
    funding = safe_float(x.get("fundingRate"))
    oi = safe_float(x.get("holdVol"))
    ticker_timestamp = x.get("timestamp")

    if price <= 0 or bid <= 0 or ask <= 0:
        continue

    spread_pct = ((ask - bid) / price) * 100

    if amount24 <= 0:
        amount24 = volume24 * price

    if amount24 < MIN_AMOUNT24:
        continue

    if spread_pct > MAX_SPREAD_PCT:
        continue

    prev = previous_oi.get(symbol, {})
    previous_value = safe_float(prev.get("oi"))

    if previous_value > 0 and oi > 0:
        oi_change_pct = ((oi - previous_value) / previous_value) * 100
    else:
        oi_change_pct = None

    candidates.append({
        "symbol": symbol,
        "price": price,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
        "volume24": volume24,
        "amount24": amount24,
        "change24": change24,
        "funding_rate": funding,
        "oi": oi,
        "oi_change_pct": oi_change_pct,
        "ticker_timestamp": ticker_timestamp,
        "ticker_time_jst": ms_to_jst(ticker_timestamp),
    })


for c in candidates:
    c["selection_score"] = (
        c["amount24"]
        * (1 + min(abs(c["change24"]), 0.50))
    )


candidates.sort(
    key=lambda x: x["selection_score"],
    reverse=True
)

candidates = candidates[:DETAIL_COUNT]

print("詳細分析対象:", len(candidates), "銘柄")
print()

results = []

print("BTC市場スナップショット取得中...")
btc_market = get_btc_market_snapshot()
print(
    "BTC:",
    round(btc_market["btc_price"], 2),
    "| 1m:",
    round(btc_market["btc_ret_1m_pct"], 4),
    "% | 5m:",
    round(btc_market["btc_ret_5m_pct"], 4),
    "% | 15m:",
    round(btc_market["btc_ret_15m_pct"], 4),
    "% | 1h:",
    round(btc_market["btc_ret_1h_pct"], 4),
    "%"
)
print()

for index, c in enumerate(candidates, 1):
    symbol = c["symbol"]

    try:
        k1 = get_klines(symbol, "Min1", 120)
        time.sleep(API_SLEEP)

        k5 = get_klines(symbol, "Min5", 120)
        time.sleep(API_SLEEP)

        k15 = get_klines(symbol, "Min15", 120)
        time.sleep(API_SLEEP)

        a1 = analyse_timeframe(k1)
        a5 = analyse_timeframe(k5)
        a15 = analyse_timeframe(k15)

        if a1 is None or a5 is None or a15 is None:
            print(f"{index:02d}/{len(candidates)}", symbol, "Kline不足")
            continue

        long_score = (
            a1["long"]
            + a5["long"] * 1.5
            + a15["long"] * 2
        )

        short_score = (
            a1["short"]
            + a5["short"] * 1.5
            + a15["short"] * 2
        )

        oi_change = c["oi_change_pct"]

        if oi_change is not None and oi_change >= 1:
            if long_score > short_score:
                long_score += 1
            elif short_score > long_score:
                short_score += 1

        funding = c["funding_rate"]

        if funding >= 0.001:
            short_score += 1
        elif funding <= -0.001:
            long_score += 1

        atr1_pct = (
            a1["atr"] / c["price"] * 100
            if a1["atr"] else 0
        )

        atr5_pct = (
            a5["atr"] / c["price"] * 100
            if a5["atr"] else 0
        )

        atr15_pct = (
            a15["atr"] / c["price"] * 100
            if a15["atr"] else 0
        )

        if long_score > short_score:
            bias = "LONG"
            edge = long_score - short_score
        elif short_score > long_score:
            bias = "SHORT"
            edge = short_score - long_score
        else:
            bias = "NEUTRAL"
            edge = 0

        result = {
            "scan_time_jst": datetime.now(JST).strftime(
                "%Y-%m-%d %H:%M:%S.%f"
            )[:-3],

            "symbol": symbol,
            "ticker_time_jst": c["ticker_time_jst"],
            "price": c["price"],
            "bid": c["bid"],
            "ask": c["ask"],
            "spread_pct": c["spread_pct"],
            "volume24": c["volume24"],
            "amount24": c["amount24"],
            "change24": c["change24"],
            "funding_rate": c["funding_rate"],
            "oi": c["oi"],
            "oi_change_pct": c["oi_change_pct"],

            "rsi1": a1["rsi"],
            "ema9_1": a1["ema9"],
            "ema21_1": a1["ema21"],
            "macd1": a1["macd"],
            "macd_signal1": a1["macd_signal"],
            "macd_hist1": a1["macd_hist"],
            "volume_ratio1": a1["volume_ratio"],
            "atr1_pct": atr1_pct,

            "rsi5": a5["rsi"],
            "ema9_5": a5["ema9"],
            "ema21_5": a5["ema21"],
            "macd5": a5["macd"],
            "macd_signal5": a5["macd_signal"],
            "macd_hist5": a5["macd_hist"],
            "volume_ratio5": a5["volume_ratio"],
            "atr5_pct": atr5_pct,
            "recent_high5": a5["recent_high"],
            "recent_low5": a5["recent_low"],
            "last_high5": a5["last_high"],
            "last_low5": a5["last_low"],
            "rsi15": a15["rsi"],
            "ema9_15": a15["ema9"],
            "ema21_15": a15["ema21"],
            "macd15": a15["macd"],
            "macd_signal15": a15["macd_signal"],
            "macd_hist15": a15["macd_hist"],
            "volume_ratio15": a15["volume_ratio"],
            "atr15_pct": atr15_pct,

            "long_score": long_score,
            "short_score": short_score,
            "bias": bias,
            "edge": edge,

            # BTC市場状態の生特徴量
            **btc_market
        }

        results.append(result)

        print(
            f"{index:02d}/{len(candidates)}",
            symbol,
            "OK",
            "|",
            bias,
            "| L:",
            round(long_score, 1),
            "S:",
            round(short_score, 1),
            "| RSI:",
            round(a1["rsi"], 1),
            "/",
            round(a5["rsi"], 1),
            "/",
            round(a15["rsi"], 1)
        )

    except Exception as e:
        print(
            f"{index:02d}/{len(candidates)}",
            symbol,
            "ERROR:",
            e
        )


long_rank = sorted(
    results,
    key=lambda x: x["long_score"] - x["short_score"],
    reverse=True
)

short_rank = sorted(
    results,
    key=lambda x: x["short_score"] - x["long_score"],
    reverse=True
)


print()
print("=" * 110)
print("LONG候補 TOP10")
print("=" * 110)

for x in long_rank[:TOP_COUNT]:
    print(
        x["symbol"],
        "|価格:",
        x["price"],
        "|L:",
        round(x["long_score"], 1),
        "|S:",
        round(x["short_score"], 1),
        "|RSI:",
        round(x["rsi1"], 1),
        "/",
        round(x["rsi5"], 1),
        "/",
        round(x["rsi15"], 1),
        "|Vol5:",
        round(x["volume_ratio5"], 2),
        "|OIΔ:",
        (
            round(x["oi_change_pct"], 2)
            if x["oi_change_pct"] is not None
            else "初回"
        ),
        "|Funding:",
        round(x["funding_rate"] * 100, 4),
        "%"
    )


print()
print("=" * 110)
print("SHORT候補 TOP10")
print("=" * 110)

for x in short_rank[:TOP_COUNT]:
    print(
        x["symbol"],
        "|価格:",
        x["price"],
        "|S:",
        round(x["short_score"], 1),
        "|L:",
        round(x["long_score"], 1),
        "|RSI:",
        round(x["rsi1"], 1),
        "/",
        round(x["rsi5"], 1),
        "/",
        round(x["rsi15"], 1),
        "|Vol5:",
        round(x["volume_ratio5"], 2),
        "|OIΔ:",
        (
            round(x["oi_change_pct"], 2)
            if x["oi_change_pct"] is not None
            else "初回"
        ),
        "|Funding:",
        round(x["funding_rate"] * 100, 4),
        "%"
    )


finish = datetime.now(JST)

filename = finish.strftime(
    "mexc_scan_%Y%m%d_%H%M%S.csv"
)

if results:
    fieldnames = list(results[0].keys())

    with open(
        filename,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames
        )

        writer.writeheader()

        for row in results:
            writer.writerow(row)


save_current_oi(current_oi_state)

print()
print("=" * 110)

print(
    "終了:",
    finish.strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3],
    "JST"
)

print("CSV保存:", filename)
print("OI状態保存:", OI_STATE_FILE)
print("=" * 110)
