"""
Commodities Board — Extended commodity tracking with full analysis.
Bloomberg equivalent: GLCO
"""
from __future__ import annotations
import json, os, time
from typing import Any
import yfinance as yf
import pandas as pd
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR, COMMODITIES_EXT
from services.net_utils import disable_dead_proxy_env

CACHE_FILE = os.path.join(CACHE_DIR, "commodities_board.json")
CACHE_TTL = 1800

def _load_cache():
    try:
        if not os.path.exists(CACHE_FILE): return None
        if time.time() - os.path.getmtime(CACHE_FILE) > CACHE_TTL: return None
        with open(CACHE_FILE, "r") as f: return json.load(f).get("data")
    except Exception: return None

def _save_cache(data):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"as_of": time.time(), "data": data}, f)
    except Exception: pass

def fetch_commodities_board() -> list[dict[str, Any]]:
    cached = _load_cache()
    if cached: return cached
    
    disable_dead_proxy_env()
    
    tickers = list(COMMODITIES_EXT.values())
    try:
        data = yf.download(
            " ".join(tickers), period="120d", interval="1d",
            group_by="ticker", progress=False, threads=False, auto_adjust=False
        )
    except Exception:
        return []
    
    rows = []
    for name, ticker in COMMODITIES_EXT.items():
        try:
            if isinstance(data.columns, pd.MultiIndex):
                if ticker not in data.columns.get_level_values(0):
                    continue
                frame = data[ticker]
            else:
                frame = data
            
            close = frame["Close"].dropna()
            high = frame["High"].dropna()
            low = frame["Low"].dropna()
            
            if close.empty or len(close) < 22:
                continue
            
            price = float(close.iloc[-1])
            chg_1d = round((price / float(close.iloc[-2]) - 1) * 100, 2) if len(close) >= 2 else 0
            chg_1w = round((price / float(close.iloc[-5]) - 1) * 100, 2) if len(close) >= 5 else 0
            chg_1m = round((price / float(close.iloc[-22]) - 1) * 100, 2) if len(close) >= 22 else 0
            
            # ATR
            prev_close = close.shift(1)
            tr = pd.concat([
                (high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()
            ], axis=1).max(axis=1)
            atr14 = float(tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1])
            
            # SMA stack
            sma20 = float(close.tail(20).mean())
            sma50 = float(close.tail(50).mean()) if len(close) >= 50 else price
            
            if price > sma20 > sma50:
                trend = "Bullish"
            elif price < sma20 < sma50:
                trend = "Bearish"
            else:
                trend = "Neutral"
            
            # 52w high/low
            high_52w = float(close.max())
            low_52w = float(close.min())
            pct_from_high = round(((price / high_52w) - 1) * 100, 2)
            
            rows.append({
                "name": name,
                "ticker": ticker,
                "price": round(price, 2),
                "chg_1d": chg_1d,
                "chg_1w": chg_1w,
                "chg_1m": chg_1m,
                "atr14": round(atr14, 2),
                "sma20": round(sma20, 2),
                "sma50": round(sma50, 2),
                "trend": trend,
                "high_range": round(high_52w, 2),
                "low_range": round(low_52w, 2),
                "pct_from_high": pct_from_high,
                "source": "yfinance",
                "is_real": True,
            })
        except Exception:
            continue
    
    if rows:
        _save_cache(rows)
    return rows
