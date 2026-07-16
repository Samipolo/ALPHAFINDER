"""Resilient single-symbol price history: yfinance first, Yahoo v8 JSON fallback.

yfinance scrapes Yahoo through a cookie/crumb handshake that Yahoo rate-limits
hard once a burst of requests goes out from one IP (exactly what happens on
Render during a refresh). Yahoo's public chart endpoint
(query1.finance.yahoo.com/v8/finance/chart) serves the same OHLCV data as plain
JSON over a completely different, lighter request path, so it keeps working when
yfinance is throttled. It also takes the *same* ticker symbols (SPY, ^VIX,
EURUSD=X, GC=F, BTC-USD), so no symbol remapping is needed.

robust_history() returns a DataFrame shaped like yf.Ticker(symbol).history():
capitalised Open/High/Low/Close/Volume columns on a DatetimeIndex. Callers that
already consume that shape need no other change.
"""
from __future__ import annotations

import pandas as pd
import requests
import yfinance as yf

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

# Map the period strings used across the app onto ranges the v8 chart endpoint
# accepts. We round up when there is no exact match -- callers slice/tail what
# they need, so a little extra history is harmless.
_V8_RANGES = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
_PERIOD_TO_RANGE = {
    "1d": "1d", "5d": "5d", "7d": "5d",
    "14d": "1mo", "1wk": "5d", "1mo": "1mo", "30d": "1mo",
    "60d": "3mo", "90d": "3mo", "3mo": "3mo", "6mo": "6mo",
    "1y": "1y", "12mo": "1y", "2y": "2y", "5y": "5y", "10y": "10y",
    "ytd": "ytd", "max": "max",
}


def _period_to_range(period: str) -> str:
    if period in _V8_RANGES:
        return period
    return _PERIOD_TO_RANGE.get(period, "1y")


def _v8_history(symbol: str, period: str, interval: str) -> pd.DataFrame:
    rng = _period_to_range(period)
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range={rng}&interval={interval}&includePrePost=false"
    )
    resp = requests.get(url, headers=_UA, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError(f"v8 returned no result for {symbol}")
    res = result[0]
    timestamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    if not timestamps or not quote:
        raise ValueError(f"v8 returned empty series for {symbol}")

    frame = pd.DataFrame(
        {
            "Open": quote.get("open"),
            "High": quote.get("high"),
            "Low": quote.get("low"),
            "Close": quote.get("close"),
            "Volume": quote.get("volume"),
        },
        index=pd.to_datetime(timestamps, unit="s", utc=True),
    )
    # adjclose, when present, is what auto_adjust=True yfinance returns as Close.
    adj = (((res.get("indicators") or {}).get("adjclose") or [{}])[0]).get("adjclose")
    if adj is not None:
        try:
            frame["Close"] = pd.Series(adj, index=frame.index).fillna(frame["Close"])
        except Exception:
            pass
    frame = frame.dropna(how="all")
    if frame.empty:
        raise ValueError(f"v8 series all-NaN for {symbol}")
    return frame


def robust_history(symbol: str, period: str = "1y", interval: str = "1d",
                   auto_adjust: bool = True) -> pd.DataFrame:
    """yfinance history with an automatic Yahoo-v8 JSON fallback.

    Returns an empty DataFrame only if BOTH providers fail, so callers keep
    their existing "empty -> skip" handling and simply stop failing whenever
    Yahoo throttles yfinance but still answers the plain chart endpoint.
    """
    try:
        df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=auto_adjust)
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    try:
        return _v8_history(symbol, period, interval)
    except Exception:
        return pd.DataFrame()