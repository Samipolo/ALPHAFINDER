"""
Correlation Matrix — 60-day rolling correlation heatmap across all assets.
Bloomberg equivalent: CORR
"""
from __future__ import annotations
import json, os, time
from typing import Any
import yfinance as yf
import pandas as pd
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR
from services.net_utils import disable_dead_proxy_env

CACHE_FILE = os.path.join(CACHE_DIR, "correlation.json")
CACHE_TTL = 3600

# Key assets for correlation matrix
CORR_ASSETS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "CAD=X",
    "SPX500": "^GSPC", "NAS100": "^NDX", "XAUUSD": "GC=F",
    "USOIL": "CL=F", "BTCUSD": "BTC-USD", "DXY": "DX-Y.NYB",
    "VIX": "^VIX",
}

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

def fetch_correlation_matrix() -> dict[str, Any]:
    cached = _load_cache()
    if cached: return cached
    
    disable_dead_proxy_env()
    
    tickers = list(CORR_ASSETS.values())
    try:
        data = yf.download(
            " ".join(tickers), period="90d", interval="1d",
            group_by="ticker", progress=False, threads=False, auto_adjust=False
        )
    except Exception:
        return {"assets": [], "matrix": {}, "source": "yfinance", "is_real": True}
    
    # Extract close prices
    closes = {}
    for name, ticker in CORR_ASSETS.items():
        try:
            if isinstance(data.columns, pd.MultiIndex):
                if ticker in data.columns.get_level_values(0):
                    s = data[ticker]["Close"].dropna()
                else:
                    continue
            else:
                s = data["Close"].dropna()
            if len(s) >= 30:
                closes[name] = s.pct_change().dropna()
        except Exception:
            continue
    
    if len(closes) < 3:
        return {"assets": list(closes.keys()), "matrix": {}, "source": "yfinance", "is_real": True}
    
    df = pd.DataFrame(closes).dropna()
    corr = df.tail(60).corr()
    
    matrix = {}
    assets = list(corr.columns)
    for a1 in assets:
        matrix[a1] = {}
        for a2 in assets:
            try:
                matrix[a1][a2] = round(float(corr.loc[a1, a2]), 3)
            except Exception:
                matrix[a1][a2] = 0
    
    result = {
        "assets": assets,
        "matrix": matrix,
        "period": "60-day rolling",
        "source": "yfinance",
        "is_real": True,
    }
    _save_cache(result)
    return result
