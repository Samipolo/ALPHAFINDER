"""
Cross-Currency Matrix — 8x8 live cross rate heatmap.
Bloomberg equivalent: FXCM
"""
from __future__ import annotations
import json, os, time
from typing import Any
import yfinance as yf
import pandas as pd
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR, CURRENCIES, FOREX_TICKERS
from services.net_utils import disable_dead_proxy_env

CACHE_FILE = os.path.join(CACHE_DIR, "cross_rates.json")
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

def fetch_cross_rates() -> dict[str, Any]:
    cached = _load_cache()
    if cached: return cached
    
    disable_dead_proxy_env()
    
    # Download all major pairs
    pairs_needed = {}
    for c1 in CURRENCIES:
        for c2 in CURRENCIES:
            if c1 == c2: continue
            sym = f"{c1}{c2}"
            if sym in FOREX_TICKERS:
                pairs_needed[sym] = FOREX_TICKERS[sym]
    
    tickers = list(set(pairs_needed.values()))
    try:
        data = yf.download(
            " ".join(tickers), period="22d", interval="1d",
            group_by="ticker", progress=False, threads=False, auto_adjust=False
        )
    except Exception:
        data = pd.DataFrame()
    
    rates = {}
    changes_1d = {}
    changes_1w = {}
    
    for sym, ticker in pairs_needed.items():
        try:
            if isinstance(data.columns, pd.MultiIndex) and ticker in data.columns.get_level_values(0):
                close = data[ticker]["Close"].dropna()
            elif "Close" in data.columns:
                close = data["Close"].dropna()
            else:
                continue
            
            if close.empty: continue
            price = float(close.iloc[-1])
            rates[sym] = round(price, 5) if not sym.endswith("JPY") else round(price, 3)
            
            if len(close) >= 2:
                changes_1d[sym] = round((price / float(close.iloc[-2]) - 1) * 100, 3)
            if len(close) >= 5:
                changes_1w[sym] = round((price / float(close.iloc[-5]) - 1) * 100, 3)
        except Exception:
            continue
    
    # Build 8x8 matrix
    matrix = {}
    for c1 in CURRENCIES:
        matrix[c1] = {}
        for c2 in CURRENCIES:
            if c1 == c2:
                matrix[c1][c2] = {"rate": 1.0, "chg_1d": 0.0, "chg_1w": 0.0}
                continue
            sym = f"{c1}{c2}"
            inv_sym = f"{c2}{c1}"
            if sym in rates:
                matrix[c1][c2] = {
                    "rate": rates[sym],
                    "chg_1d": changes_1d.get(sym, 0),
                    "chg_1w": changes_1w.get(sym, 0),
                }
            elif inv_sym in rates and rates[inv_sym] != 0:
                matrix[c1][c2] = {
                    "rate": round(1.0 / rates[inv_sym], 5),
                    "chg_1d": -changes_1d.get(inv_sym, 0),
                    "chg_1w": -changes_1w.get(inv_sym, 0),
                }
            else:
                matrix[c1][c2] = {"rate": None, "chg_1d": None, "chg_1w": None}
    
    result = {
        "currencies": CURRENCIES,
        "matrix": matrix,
        "source": "yfinance",
        "is_real": True,
    }
    _save_cache(result)
    return result
