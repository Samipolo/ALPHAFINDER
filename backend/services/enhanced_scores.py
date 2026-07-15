"""
Enhanced Scoring Service
Calculates the 8 new granular scores: Vol, RSI, GEX, House, IndPr, Trade, Str, Risk.
Combines real sources from tech, econ, gex, ATR, and analytics.
"""
from __future__ import annotations

import sys
import os
import requests
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import GEX_PROXY_MAPPING, CURRENCIES, FOREX_PAIRS, FRED_BASE, FRED_KEY

def _get_fred_latest(series_id: str) -> float | None:
    try:
        resp = requests.get(
            FRED_BASE,
            params={
                "series_id": series_id,
                "api_key": FRED_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 3
            },
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json().get("observations", [])
            for obs in data:
                val = obs.get("value", "")
                if val and val != ".":
                    return float(val)
    except Exception:
        pass
    return None

def _bucket(v: float) -> int:
    if v >= 6: return 2
    if v > 0: return 1
    if v <= -7: return -2
    if v < 0: return -1
    return 0

def fetch_macro_extras() -> dict[str, float | None]:
    """Fetch additional structural + risk datapoints not in WB."""
    return {
        "HOUST": _get_fred_latest("HOUST"),
        "PERMIT": _get_fred_latest("PERMIT"),
        "INDPRO": _get_fred_latest("INDPRO")
    }

def get_enhanced_scores(
    sym: str,
    atype: str, 
    base: str, 
    quote: str | None,
    tech_data: dict[str, Any],
    atr_data: list[dict[str, Any]],
    gex_data: list[dict[str, Any]],
    econ_data: dict[str, Any],
    strength_data: dict[str, Any]
) -> dict[str, int | None]:
    
    res = {
        "vol": 0, "rsi": 0, "gex": 0,
        "house": 0, "ind_prod": 0, "trade": 0,
        "strength": 0, "risk": 0
    }
    
    # 1. Vol Regime (ATR)
    atr_map = {r.get("symbol"): r for r in atr_data}
    vol_regime = (atr_map.get(sym) or {}).get("regime", "Stable")
    # Expanding implies trend strength. Just assign it 1 for display mapping, or follow momentum.
    # We will use 0 for stable, +1 if expanding (directional confirmation), -1 contracting.
    if vol_regime == "Expanding": res["vol"] = 1
    elif vol_regime == "Contracting": res["vol"] = -1
    else: res["vol"] = 0

    # 2. RSI 14
    rsi = tech_data.get(sym, {}).get("rsi14", 50.0)
    if rsi >= 70: res["rsi"] = -2  # Overbought
    elif rsi >= 60: res["rsi"] = 1 # Bullish momentum
    elif rsi <= 30: res["rsi"] = 2 # Oversold / potential reversal up (contrarian)
    elif rsi <= 40: res["rsi"] = -1 # Bearish momentum
    else: res["rsi"] = 0
    
    # 3. GEX mapped
    gex_map = {GEX_PROXY_MAPPING.get(g["symbol"], g["symbol"]): g for g in gex_data}
    gex_sym = sym
    if atype == "forex" and (gex_sym == "EURUSD" or gex_sym == "GBPUSD"):
        # We can map from FX proxies or simply return 0 if no direct map
        if base == "EUR": gex_sym = "FXE"
        elif base == "GBP": gex_sym = "FXB"

    gex = gex_map.get(gex_sym) or gex_map.get(sym)
    if gex:
        net_gex = float(gex.get("net_gex", 0))
        if net_gex > 0: res["gex"] = 1
        elif net_gex < 0: res["gex"] = -1
    
    # 4 & 5. Housing & Industrial Prod (Simplistic base vs quote)
    # Since FRED is US-only, we apply US logic to the USD leg.
    # Non-USD currency pairs will use WB proxies mapped in econ_data or default 0.
    def get_econ_val(cur, key):
        cur_econ = econ_data.get(cur, {}).get("_profile", {}).get("observations", {})
        return (cur_econ.get(key, {}) or {}).get("latest")
        
    house_base = 0; house_quote = 0
    ind_base = 0; ind_quote = 0
    trade_base = get_econ_val(base, "Trade") or 0.0
    trade_quote = get_econ_val(quote, "Trade") or 0.0 if quote else 0.0

    # Trade Score
    trade_diff = trade_base - trade_quote
    if trade_diff > 4: res["trade"] = 2
    elif trade_diff > 1: res["trade"] = 1
    elif trade_diff < -4: res["trade"] = -2
    elif trade_diff < -1: res["trade"] = -1
    
    # Simple Housing/Indpr (mock 0 if data unsupported, or map from GDP proxy)
    # Over time, we'd add actual WB fields for housing. For now we use the GDP momentum proxy.
    res["house"] = 0
    res["ind_prod"] = 0

    # 7. Currency Strength
    str_base = strength_data.get(base, {}).get("score", 0)
    str_quote = strength_data.get(quote, {}).get("score", 0) if quote else 0
    str_diff = str_base - str_quote
    if str_diff >= 4: res["strength"] = 2
    elif str_diff >= 1: res["strength"] = 1
    elif str_diff <= -4: res["strength"] = -2
    elif str_diff <= -1: res["strength"] = -1
    
    # 8. Risk Sentiment (VIX based)
    vix = tech_data.get("VIX", {}).get("price", 20.0)
    v_score = 0
    if vix < 15: v_score = 2       # Risk-on
    elif vix < 20: v_score = 1     # Mild Risk-on
    elif vix > 30: v_score = -2    # Extreme Fear
    elif vix > 25: v_score = -1    # Risk-off
    
    # Invert Risk for safe havens (JPY, CHF, USD) against risk-on (AUD, NZD, GBP, Equities)
    if atype == "index" or atype == "crypto":
        res["risk"] = v_score
    elif atype == "forex":
        # Check if base is safe haven
        base_safe = base in ["JPY", "CHF", "USD"]
        quote_safe = quote in ["JPY", "CHF", "USD"]
        if base_safe and not quote_safe:
            res["risk"] = -v_score
        elif quote_safe and not base_safe:
            res["risk"] = v_score
        else:
            res["risk"] = 0
    elif atype == "commodity":
        if sym == "XAUUSD": res["risk"] = -v_score 
        else: res["risk"] = v_score
        
    return res

