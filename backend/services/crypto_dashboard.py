"""
Crypto Dashboard — BTC/ETH/SOL + Fear & Greed Index.
Bloomberg equivalent: CRYP
"""
from __future__ import annotations
import json, os, time, requests
from typing import Any
import yfinance as yf
import pandas as pd
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR, CRYPTO_EXT
from services.net_utils import disable_dead_proxy_env

CACHE_FILE = os.path.join(CACHE_DIR, "crypto_dash.json")
CACHE_TTL = 1200

FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=7"

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

def _fetch_fear_greed():
    try:
        resp = requests.get(FEAR_GREED_URL, timeout=8)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            if data:
                latest = data[0]
                return {
                    "value": int(latest.get("value", 50)),
                    "label": latest.get("value_classification", "Neutral"),
                    "history": [{"value": int(d.get("value", 50)), "date": d.get("timestamp", "")} for d in data],
                }
    except Exception:
        pass
    return {"value": 50, "label": "Neutral", "history": []}

def fetch_crypto_dashboard() -> dict[str, Any]:
    cached = _load_cache()
    if cached: return cached
    
    disable_dead_proxy_env()
    
    tickers = list(CRYPTO_EXT.values())
    tickers.append("^GSPC")  # SPX for correlation
    
    try:
        data = yf.download(
            " ".join(tickers), period="90d", interval="1d",
            group_by="ticker", progress=False, threads=False, auto_adjust=False
        )
    except Exception:
        data = pd.DataFrame()
    
    coins = []
    btc_returns = None
    
    for name, ticker in CRYPTO_EXT.items():
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
            
            # Trend
            sma20 = float(close.tail(20).mean())
            sma50 = float(close.tail(50).mean()) if len(close) >= 50 else price
            
            if price > sma20 > sma50:
                trend = "Bullish"
            elif price < sma20 < sma50:
                trend = "Bearish"
            else:
                trend = "Neutral"
            
            # RSI
            delta = close.diff()
            gain = delta.where(delta > 0, 0.0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss
            rsi = float(100 - (100 / (1 + rs)).iloc[-1])
            
            if name == "BTC":
                btc_returns = close.pct_change().dropna()
            
            coins.append({
                "name": name,
                "ticker": ticker,
                "price": round(price, 2),
                "chg_1d": chg_1d,
                "chg_1w": chg_1w,
                "chg_1m": chg_1m,
                "trend": trend,
                "rsi14": round(rsi, 1),
                "is_real": True,
            })
        except Exception:
            continue
    
    # BTC-SPX Correlation
    btc_spx_corr = None
    try:
        if btc_returns is not None and isinstance(data.columns, pd.MultiIndex):
            if "^GSPC" in data.columns.get_level_values(0):
                spx_close = data["^GSPC"]["Close"].dropna()
                spx_returns = spx_close.pct_change().dropna()
                joined = pd.concat([btc_returns.rename("btc"), spx_returns.rename("spx")], axis=1).dropna()
                if len(joined) >= 30:
                    btc_spx_corr = round(float(joined.tail(60).corr().loc["btc", "spx"]), 3)
    except Exception:
        pass
    
    fear_greed = _fetch_fear_greed()
    
    result = {
        "coins": coins,
        "fear_greed": fear_greed,
        "btc_spx_corr": btc_spx_corr,
        "source": "yfinance + alternative.me",
        "is_real": True,
    }
    _save_cache(result)
    return result
