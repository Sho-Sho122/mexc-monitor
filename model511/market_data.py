"""Public market-data GETs only. No credentials, signing, or order endpoints."""
from datetime import timedelta
from urllib.parse import quote
import requests
from .config import bucket, dt, iso
from .source_reader import number


class PublicMarketData:
    def __init__(self):
        self.session = requests.Session()

    def get(self, path, params=None):
        response = self.session.get("https://api.mexc.com/api/v1/contract/" + path,
                                    params=params, timeout=20)
        response.raise_for_status()
        data = response.json()
        if data.get("success") is False:
            raise RuntimeError("Public market data request failed")
        return data.get("data")

    def candles(self, symbol, start, end, minutes):
        rows = {}
        cursor = dt(start)
        end = dt(end)
        while cursor < end:
            stop = min(cursor + timedelta(hours=6), end)
            data = self.get("kline/" + quote(symbol, safe=""), {
                "interval": "Min" + str(minutes), "start": int(cursor.timestamp()),
                "end": int(stop.timestamp())})
            if not isinstance(data, dict):
                raise ValueError("Invalid kline response")
            arrays = [data.get(k, []) for k in ("time", "open", "close", "high", "low")]
            if len({len(a) for a in arrays}) != 1:
                raise ValueError("Misaligned kline arrays")
            for t, op, cl, hi, lo in zip(*arrays):
                ts = dt(__import__("datetime").datetime.fromtimestamp(int(t),
                        tz=__import__("datetime").timezone.utc))
                values = [number(x) for x in (op, cl, hi, lo)]
                if any(x is None or x <= 0 for x in values):
                    raise ValueError("Invalid candle price")
                op, cl, hi, lo = values
                if not lo <= min(op, cl) <= max(op, cl) <= hi:
                    raise ValueError("Invalid candle range")
                if dt(start) <= ts < end:
                    rows[ts] = {"start": iso(ts), "end": iso(ts+timedelta(minutes=minutes)),
                                "open": op, "close": cl, "high": hi, "low": lo,
                                "source": "MEXC_KLINE_Min" + str(minutes)}
            cursor = stop
        return [rows[t] for t in sorted(rows)]

    def fetch(self, symbol, start, asof, stop=None):
        # All open events for a symbol share the same response/history.
        end = min(dt(asof), dt(stop)) if stop else dt(asof)
        bars = self.candles(symbol, bucket(start), end, 5)
        fine = self.candles(symbol, bucket(start), end, 1)
        data = self.get("ticker", {"symbol": symbol})
        if isinstance(data, list):
            data = next((r for r in data if r.get("symbol") == symbol), {})
        observations = []
        if isinstance(data, dict) and data.get("timestamp"):
            from datetime import datetime, timezone
            when = datetime.fromtimestamp(float(data["timestamp"])/1000, tz=timezone.utc)
            price = number(data.get("lastPrice"))
            if price is not None and price > 0:
                observations.append({"timestamp": iso(when), "price": price,
                    "bid": number(data.get("bid1")), "ask": number(data.get("ask1")),
                    "source": "MEXC_TICKER"})
        return {"bars": bars, "fine_bars": fine, "observations": observations}

