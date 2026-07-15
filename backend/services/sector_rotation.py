"""
Sector Rotation — S&P 500 sector ETF performance tracking.
Bloomberg equivalent: RV (Relative Value)
"""
from __future__ import annotations
import json, os, time
from typing import Any
import yfinance as yf
import pandas as pd
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR, SECTOR_ETFS
from services.net_utils import disable_dead_proxy_env

CACHE_FILE = os.path.join(CACHE_DIR, "sector_rotation.json")
CACHE_TTL = 3600

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

def fetch_sector_rotation() -> list[dict[str, Any]]:
    cached = _load_cache()
    if cached: return cached
    
    disable_dead_proxy_env()
    
    tickers = list(SECTOR_ETFS.keys())
    tickers.append("SPY")  # Benchmark
    
    try:
        data = yf.download(
            " ".join(tickers), period="250d", interval="1d",
            group_by="ticker", progress=False, threads=False, auto_adjust=False
        )
    except Exception:
        return []
    
    # SPY benchmark
    spy_close = None
    try:
        if isinstance(data.columns, pd.MultiIndex) and "SPY" in data.columns.get_level_values(0):
            spy_close = data["SPY"]["Close"].dropna()
        elif "Close" in data.columns:
            spy_close = data["Close"].dropna()
    except Exception:
        pass
    
    rows = []
    for ticker, sector_name in SECTOR_ETFS.items():
        try:
            if isinstance(data.columns, pd.MultiIndex):
                if ticker not in data.columns.get_level_values(0):
                    continue
                close = data[ticker]["Close"].dropna()
            else:
                close = data["Close"].dropna()
            
            if close.empty or len(close) < 22:
                continue
            
            price = float(close.iloc[-1])
            chg_1d = round((price / float(close.iloc[-2]) - 1) * 100, 2) if len(close) >= 2 else 0
            chg_1w = round((price / float(close.iloc[-5]) - 1) * 100, 2) if len(close) >= 5 else 0
            chg_1m = round((price / float(close.iloc[-22]) - 1) * 100, 2) if len(close) >= 22 else 0
            chg_3m = round((price / float(close.iloc[-66]) - 1) * 100, 2) if len(close) >= 66 else 0
            chg_ytd = round((price / float(close.iloc[0]) - 1) * 100, 2)
            
            # Relative strength vs SPY
            rel_1m = None
            if spy_close is not None and len(spy_close) >= 22:
                spy_1m = (float(spy_close.iloc[-1]) / float(spy_close.iloc[-22]) - 1) * 100
                rel_1m = round(chg_1m - spy_1m, 2)
            
            # Momentum score
            sma20 = float(close.tail(20).mean())
            sma50 = float(close.tail(50).mean()) if len(close) >= 50 else price
            
            if price > sma20 > sma50:
                momentum = "Strong"
            elif price > sma20:
                momentum = "Moderate"
            elif price < sma20 < sma50:
                momentum = "Weak"
            else:
                momentum = "Mixed"
            
            rows.append({
                "ticker": ticker,
                "sector": sector_name,
                "price": round(price, 2),
                "chg_1d": chg_1d,
                "chg_1w": chg_1w,
                "chg_1m": chg_1m,
                "chg_3m": chg_3m,
                "chg_ytd": chg_ytd,
                "rel_1m": rel_1m,
                "momentum": momentum,
                "source": "yfinance",
                "is_real": True,
            })
        except Exception:
            continue
    
    rows.sort(key=lambda x: x.get("chg_1m", 0), reverse=True)
    if rows:
        _save_cache(rows)
    return rows
