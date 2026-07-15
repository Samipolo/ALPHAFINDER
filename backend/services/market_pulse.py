"""
Market Pulse Monitor - cross-asset surveillance built from live Yahoo Finance data.
Bloomberg inspiration: market monitors, cross-asset boards, and fast movers.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR  # noqa: E402
from services.net_utils import disable_dead_proxy_env  # noqa: E402

disable_dead_proxy_env()
import yfinance as yf  # noqa: E402


CACHE_FILE = os.path.join(CACHE_DIR, "market_pulse.json")
CACHE_TTL = 300

UNIVERSE = [
    {"symbol": "SPX", "ticker": "^GSPC", "label": "S&P 500", "group": "Equities"},
    {"symbol": "NDX", "ticker": "^NDX", "label": "Nasdaq 100", "group": "Equities"},
    {"symbol": "DJI", "ticker": "^DJI", "label": "Dow Jones", "group": "Equities"},
    {"symbol": "RUT", "ticker": "^RUT", "label": "Russell 2000", "group": "Equities"},
    {"symbol": "FTSE", "ticker": "^FTSE", "label": "FTSE 100", "group": "Equities"},
    {"symbol": "DAX", "ticker": "^GDAXI", "label": "DAX", "group": "Equities"},
    {"symbol": "NIKKEI", "ticker": "^N225", "label": "Nikkei 225", "group": "Equities"},
    {"symbol": "EURUSD", "ticker": "EURUSD=X", "label": "EUR/USD", "group": "FX"},
    {"symbol": "GBPUSD", "ticker": "GBPUSD=X", "label": "GBP/USD", "group": "FX"},
    {"symbol": "USDJPY", "ticker": "JPY=X", "label": "USD/JPY", "group": "FX"},
    {"symbol": "AUDUSD", "ticker": "AUDUSD=X", "label": "AUD/USD", "group": "FX"},
    {"symbol": "DXY", "ticker": "DX-Y.NYB", "label": "US Dollar Index", "group": "FX"},
    {"symbol": "US10Y", "ticker": "^TNX", "label": "US 10Y Yield", "group": "Rates/Vol"},
    {"symbol": "VIX", "ticker": "^VIX", "label": "VIX", "group": "Rates/Vol"},
    {"symbol": "VIX3M", "ticker": "^VIX3M", "label": "VIX 3M", "group": "Rates/Vol"},
    {"symbol": "XAUUSD", "ticker": "GC=F", "label": "Gold", "group": "Commodities"},
    {"symbol": "XAGUSD", "ticker": "SI=F", "label": "Silver", "group": "Commodities"},
    {"symbol": "USOIL", "ticker": "CL=F", "label": "WTI Crude", "group": "Commodities"},
    {"symbol": "COPPER", "ticker": "HG=F", "label": "Copper", "group": "Commodities"},
    {"symbol": "NATGAS", "ticker": "NG=F", "label": "Nat Gas", "group": "Commodities"},
    {"symbol": "BTCUSD", "ticker": "BTC-USD", "label": "Bitcoin", "group": "Crypto"},
    {"symbol": "ETHUSD", "ticker": "ETH-USD", "label": "Ethereum", "group": "Crypto"},
    {"symbol": "SOLUSD", "ticker": "SOL-USD", "label": "Solana", "group": "Crypto"},
]


def _load_cache() -> dict[str, Any] | None:
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        if time.time() - os.path.getmtime(CACHE_FILE) > CACHE_TTL:
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle).get("data")
    except Exception:
        return None


def _save_cache(data: dict[str, Any]) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(
                {"as_of": datetime.now(timezone.utc).isoformat(), "data": data},
                handle,
            )
    except Exception:
        pass


def _configure_yf_cache() -> None:
    try:
        yf.set_tz_cache_location(CACHE_DIR)
    except Exception:
        pass
    try:
        import yfinance.cache as yf_cache

        yf_cache.set_cache_location(CACHE_DIR)
    except Exception:
        pass


def _extract_close(frame: pd.DataFrame, ticker: str, single: bool) -> pd.Series | None:
    try:
        if single:
            close = frame["Close"]
        else:
            if ticker not in frame.columns.get_level_values(0):
                return None
            close = frame[ticker]["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        return close if len(close) >= 2 else None
    except Exception:
        return None


def _pct_change(close: pd.Series, periods: int) -> float | None:
    if len(close) <= periods:
        return None
    base = float(close.iloc[-(periods + 1)])
    latest = float(close.iloc[-1])
    if not base:
        return None
    return round(((latest / base) - 1) * 100, 3)


def _rsi14(close: pd.Series) -> float | None:
    if len(close) < 15:
        return None
    delta = close.diff(1)
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    value = float(rsi.iloc[-1])
    if value != value:
        return None
    return round(value, 2)


def _trend_label(close: pd.Series) -> str:
    sma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else float(close.iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else sma20
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else sma50
    latest = float(close.iloc[-1])
    if latest > sma20 > sma50 > sma200:
        return "Strong Bullish"
    if latest > sma50 > sma200:
        return "Bullish"
    if latest < sma20 < sma50 < sma200:
        return "Strong Bearish"
    if latest < sma50 < sma200:
        return "Bearish"
    return "Rangebound"


def _market_regime(records: dict[str, dict[str, Any]]) -> str:
    spx = records.get("SPX", {})
    ndx = records.get("NDX", {})
    vix = records.get("VIX", {})
    dxy = records.get("DXY", {})
    us10y = records.get("US10Y", {})
    signals = 0

    if (spx.get("chg_1d") or 0) > 0:
        signals += 1
    else:
        signals -= 1
    if (ndx.get("chg_1d") or 0) > 0:
        signals += 1
    else:
        signals -= 1
    if (vix.get("chg_1d") or 0) < 0:
        signals += 1
    else:
        signals -= 1
    if (dxy.get("chg_1d") or 0) < 0:
        signals += 1
    else:
        signals -= 1
    if (us10y.get("chg_5d") or 0) < 0:
        signals += 1
    else:
        signals -= 1

    if signals >= 3:
        return "Risk-On Rotation"
    if signals <= -3:
        return "Risk-Off Defensive"
    return "Mixed Cross-Asset Tape"


def fetch_market_pulse() -> dict[str, Any]:
    cached = _load_cache()
    if cached:
        return cached

    disable_dead_proxy_env()
    _configure_yf_cache()
    tickers = [item["ticker"] for item in UNIVERSE]
    frame = yf.download(
        " ".join(tickers),
        period="1y",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=False,
    )

    grouped: dict[str, list[dict[str, Any]]] = {}
    records: dict[str, dict[str, Any]] = {}
    single = len(tickers) == 1

    for item in UNIVERSE:
        close = _extract_close(frame, item["ticker"], single)
        if close is None:
            continue

        latest = float(close.iloc[-1])
        latest_dt = close.index[-1]
        row = {
            "symbol": item["symbol"],
            "label": item["label"],
            "ticker": item["ticker"],
            "group": item["group"],
            "price": round(latest, 4),
            "chg_1d": _pct_change(close, 1),
            "chg_5d": _pct_change(close, 5),
            "chg_1m": _pct_change(close, 21),
            "chg_3m": _pct_change(close, 63),
            "rsi14": _rsi14(close),
            "trend": _trend_label(close),
            "as_of": latest_dt.strftime("%Y-%m-%d"),
        }
        grouped.setdefault(item["group"], []).append(row)
        records[item["symbol"]] = row

    ordered_groups = {
        name: grouped.get(name, [])
        for name in ("Equities", "FX", "Rates/Vol", "Commodities", "Crypto")
    }
    all_rows = [row for rows in ordered_groups.values() for row in rows]
    leaders = sorted(
        [row for row in all_rows if row.get("chg_1d") is not None],
        key=lambda item: float(item["chg_1d"]),
        reverse=True,
    )[:6]
    laggards = sorted(
        [row for row in all_rows if row.get("chg_1d") is not None],
        key=lambda item: float(item["chg_1d"]),
    )[:6]
    positive_1d = sum(1 for row in all_rows if (row.get("chg_1d") or 0) > 0)
    positive_1m = sum(1 for row in all_rows if (row.get("chg_1m") or 0) > 0)

    result = {
        "groups": ordered_groups,
        "leaders": leaders,
        "laggards": laggards,
        "breadth": {
            "total": len(all_rows),
            "positive_1d": positive_1d,
            "positive_1m": positive_1m,
            "positive_1d_pct": round((positive_1d / len(all_rows)) * 100, 1) if all_rows else 0.0,
            "positive_1m_pct": round((positive_1m / len(all_rows)) * 100, 1) if all_rows else 0.0,
        },
        "regime": _market_regime(records),
        "source": "Yahoo Finance / yfinance",
        "is_real": True,
    }
    _save_cache(result)
    return result
