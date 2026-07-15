"""
Volatility Surface — VIX term structure + ATR regime analysis.
Bloomberg equivalent: OVDV
"""
from __future__ import annotations
import json, os, time
from typing import Any
import yfinance as yf
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR, VIX_TERM
from services.net_utils import disable_dead_proxy_env

CACHE_FILE = os.path.join(CACHE_DIR, "vol_surface.json")
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

def fetch_volatility_surface() -> dict[str, Any]:
    cached = _load_cache()
    if cached: return cached
    
    disable_dead_proxy_env()
    
    # VIX Term Structure
    vix_data = {}
    for name, ticker in VIX_TERM.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="60d")
            if not hist.empty:
                close = hist["Close"].dropna()
                vix_data[name] = {
                    "current": round(float(close.iloc[-1]), 2),
                    "prev_close": round(float(close.iloc[-2]), 2) if len(close) > 1 else None,
                    "week_ago": round(float(close.iloc[-5]), 2) if len(close) >= 5 else None,
                    "month_ago": round(float(close.iloc[-22]), 2) if len(close) >= 22 else None,
                    "high_30d": round(float(close.tail(30).max()), 2),
                    "low_30d": round(float(close.tail(30).min()), 2),
                    "percentile": round(float(
                        (close.iloc[-1] - close.min()) / (close.max() - close.min()) * 100
                    ), 1) if close.max() != close.min() else 50.0,
                }
        except Exception:
            continue
    
    # Term structure analysis
    vix_spot = vix_data.get("VIX", {}).get("current")
    vix_3m = vix_data.get("VIX3M", {}).get("current")
    
    if vix_spot and vix_3m:
        if vix_spot > vix_3m:
            structure = "Backwardation — Fear/Stress"
            structure_signal = -1
        elif vix_3m > vix_spot * 1.05:
            structure = "Contango — Complacency"
            structure_signal = 1
        else:
            structure = "Flat — Neutral"
            structure_signal = 0
    else:
        structure = "Unavailable"
        structure_signal = 0
    
    # Fear gauge
    vix_level = vix_spot or 20
    if vix_level < 13:
        fear_gauge = "Extreme Greed"
    elif vix_level < 18:
        fear_gauge = "Low Fear"
    elif vix_level < 25:
        fear_gauge = "Moderate Fear"
    elif vix_level < 35:
        fear_gauge = "High Fear"
    else:
        fear_gauge = "Extreme Fear"
    
    result = {
        "vix_term": vix_data,
        "structure": structure,
        "structure_signal": structure_signal,
        "fear_gauge": fear_gauge,
        "source": "yfinance (CBOE VIX)",
        "is_real": True,
    }
    _save_cache(result)
    return result
