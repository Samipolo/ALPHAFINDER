"""Chart Terminal data service - real OHLCV with orderflow-style analytics.

Sources: Yahoo Finance for stocks/ETFs/FX/futures; Binance public klines for
crypto intraday (real-time, keyless). Everything derived (volume delta,
cumulative delta, volume profile, bands, walls) is computed from those bars.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from services.price_data import robust_history

CHART_SYMBOLS = [
    "SPY", "QQQ", "DIA", "IWM", "NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META",
    "GLD", "SLV", "USO", "TLT", "^VIX",
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "BTC-USD", "ETH-USD", "SOL-USD",
]

BINANCE_MAP = {"BTC-USD": "BTCUSDT", "ETH-USD": "ETHUSDT", "SOL-USD": "SOLUSDT"}

INTERVALS = {
    "1d": {"yf_interval": "1d", "yf_period": "2y", "binance": "1d", "limit": 500},
    "1h": {"yf_interval": "1h", "yf_period": "60d", "binance": "1h", "limit": 600},
    "15m": {"yf_interval": "15m", "yf_period": "30d", "binance": "15m", "limit": 600},
    "5m": {"yf_interval": "5m", "yf_period": "14d", "binance": "5m", "limit": 600},
}

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()
_TTL = {"1d": 300, "1h": 120, "15m": 60, "5m": 45}


def _bars_binance(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    url = ("https://api.binance.com/api/v3/klines"
           f"?symbol={BINANCE_MAP[symbol]}&interval={interval}&limit={min(limit, 1000)}")
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    rows = r.json()
    df = pd.DataFrame(rows, columns=[
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "qv", "trades", "taker_base", "taker_quote", "ignore"])
    for c in ("Open", "High", "Low", "Close", "Volume", "taker_base"):
        df[c] = df[c].astype(float)
    df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df

def _bars_yf(symbol: str, spec: dict) -> pd.DataFrame:
    df = robust_history(symbol, period=spec["yf_period"],
                        interval=spec["yf_interval"], auto_adjust=True)
    if df is None or df.empty:
        raise ValueError(f"no bars for {symbol}")
    return df


def fetch_chart(symbol: str = "SPY", interval: str = "1d") -> dict[str, Any]:
    if symbol not in CHART_SYMBOLS:
        symbol = "SPY"
    if interval not in INTERVALS:
        interval = "1d"
    key = f"{symbol}|{interval}"
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _TTL[interval]:
            return hit[1]

    spec = INTERVALS[interval]
    binance_taker = None
    if symbol in BINANCE_MAP:
        df = _bars_binance(symbol, spec["binance"], spec["limit"])
        binance_taker = df["taker_base"].to_numpy()
        source = "Binance spot klines (real-time)"
    else:
        df = _bars_yf(symbol, spec)
        source = "Yahoo Finance OHLCV"
    df = df.tail(spec["limit"])

    o = df["Open"].to_numpy(float)
    h = df["High"].to_numpy(float)
    l = df["Low"].to_numpy(float)
    c = df["Close"].to_numpy(float)
    v = df["Volume"].fillna(0).to_numpy(float) if "Volume" in df else np.zeros(len(c))

    if interval == "1d":
        times = [str(ts.date()) for ts in df.index]
    else:
        times = [int(ts.timestamp()) for ts in df.index]

    bars = [{"time": t, "open": round(o[i], 6), "high": round(h[i], 6),
             "low": round(l[i], 6), "close": round(c[i], 6),
             "volume": round(v[i], 2)} for i, t in enumerate(times)]

    # ── orderflow-style volume delta ──
    if binance_taker is not None:
        buy = binance_taker
        sell = v - buy
        delta = buy - sell
        delta_kind = "true taker buy/sell (Binance)"
    else:
        span = np.where(h - l > 0, h - l, 1e-12)
        buy_frac = np.clip((c - l) / span, 0, 1)
        delta = v * (2 * buy_frac - 1)
        delta_kind = "close-location proxy from OHLCV"
    cum = np.cumsum(delta)
    delta_series = [{"time": t, "value": round(delta[i], 2),
                     "color": "#00e5a0" if delta[i] >= 0 else "#ff2d55"}
                    for i, t in enumerate(times)]
    cum_series = [{"time": t, "value": round(cum[i], 2)} for i, t in enumerate(times)]

    # ── moving averages / bands ──
    cs = pd.Series(c)
    def _line(vals):
        return [{"time": times[i], "value": round(float(x), 6)}
                for i, x in enumerate(vals) if not math.isnan(x)]
    sma50 = _line(cs.rolling(50).mean())
    sma200 = _line(cs.rolling(200).mean())
    m20 = cs.rolling(20).mean()
    s20 = cs.rolling(20).std()
    bb_up = _line(m20 + 2 * s20)
    bb_dn = _line(m20 - 2 * s20)
    vwap = []
    if interval != "1d" and v.sum() > 0:
        typ = (h + l + c) / 3
        dates = [ts.date() for ts in df.index]
        acc_pv = acc_v = 0.0
        cur = None
        vals = []
        for i in range(len(c)):
            if dates[i] != cur:
                cur, acc_pv, acc_v = dates[i], 0.0, 0.0
            acc_pv += typ[i] * v[i]
            acc_v += v[i]
            vals.append(acc_pv / acc_v if acc_v else typ[i])
        vwap = _line(np.array(vals))

    # ── volume profile ──
    profile = []
    if v.sum() > 0:
        lo, hi = float(l.min()), float(h.max())
        nb = 28
        edges = np.linspace(lo, hi, nb + 1)
        mid = (edges[:-1] + edges[1:]) / 2
        buckets = np.zeros(nb)
        idx = np.clip(np.digitize(c, edges) - 1, 0, nb - 1)
        for i, b in enumerate(idx):
            buckets[b] += v[i]
        poc = float(mid[int(buckets.argmax())])
        profile = [{"price": round(float(mid[i]), 4), "volume": round(float(buckets[i]), 2)}
                   for i in range(nb)]
    else:
        poc = None

    # ── quant levels ──
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    atr = float(pd.Series(tr).rolling(14).mean().iloc[-1]) if len(tr) >= 14 else None
    rets = np.diff(np.log(c))
    sd = float(np.std(rets[-100:])) if len(rets) > 20 else None
    last = float(c[-1])
    levels = {
        "last": round(last, 6),
        "atr14": round(atr, 6) if atr else None,
        "atr_high": round(last + atr, 6) if atr else None,
        "atr_low": round(last - atr, 6) if atr else None,
        "sigma1_up": round(last * math.exp(sd), 6) if sd else None,
        "sigma1_dn": round(last * math.exp(-sd), 6) if sd else None,
        "poc": round(poc, 4) if poc else None,
    }
    if interval == "1d":
        try:
            from services.options_flow import fetch_options_flow, FLOW_SYMBOLS
            if symbol in FLOW_SYMBOLS:
                flow = fetch_options_flow(symbol)
                front = (flow.get("expiries") or [{}])[0]
                levels["call_wall"] = front.get("call_wall")
                levels["put_wall"] = front.get("put_wall")
                levels["max_pain"] = front.get("max_pain")
                levels["gamma_flip"] = flow.get("gamma_flip_est")
        except Exception:
            pass

    result = {
        "symbol": symbol, "interval": interval, "symbols": CHART_SYMBOLS,
        "bars": bars, "sma50": sma50, "sma200": sma200,
        "bb_up": bb_up, "bb_dn": bb_dn, "vwap": vwap,
        "delta": delta_series, "cum_delta": cum_series, "delta_kind": delta_kind,
        "volume_profile": profile, "levels": levels,
        "source": source,
    }
    with _CACHE_LOCK:
        _CACHE[key] = (now, result)
    return result

def fetch_depth(symbol: str = "BTC-USD") -> dict[str, Any]:
    """Live order book (L2 depth) — Binance public API, crypto only."""
    if symbol not in BINANCE_MAP:
        return {"symbol": symbol, "available": False,
                "note": "Free L2 depth exists only for crypto (Binance). Equity/FX DOM requires a paid feed."}
    url = f"https://api.binance.com/api/v3/depth?symbol={BINANCE_MAP[symbol]}&limit=500"
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    book = r.json()
    bids = [(float(p), float(q)) for p, q in book.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in book.get("asks", [])]
    if not bids or not asks:
        raise ValueError("empty book")
    mid = (bids[0][0] + asks[0][0]) / 2

    def _bucket(rows, n=22):
        lo = min(p for p, _ in rows)
        hi = max(p for p, _ in rows)
        if hi <= lo:
            hi = lo * 1.0001
        edges = np.linspace(lo, hi, n + 1)
        out = np.zeros(n)
        for p, q in rows:
            i = min(int((p - lo) / (hi - lo) * n), n - 1)
            out[i] += p * q
        mids = (edges[:-1] + edges[1:]) / 2
        return [{"price": round(float(mids[i]), 2), "usd": round(float(out[i]), 0)}
                for i in range(n) if out[i] > 0]

    bid_usd = sum(p * q for p, q in bids)
    ask_usd = sum(p * q for p, q in asks)
    return {
        "symbol": symbol, "available": True, "mid": round(mid, 2),
        "bids": _bucket(bids), "asks": _bucket(asks),
        "bid_usd": round(bid_usd), "ask_usd": round(ask_usd),
        "imbalance_pct": round((bid_usd - ask_usd) / (bid_usd + ask_usd) * 100, 1),
        "levels": len(bids) + len(asks),
        "source": "Binance L2 depth (live, top 500 levels/side)",
    }