"""
Rate Differentials — Central bank interest rate spreads.
Bloomberg equivalent: WIRP
"""
from __future__ import annotations
import json, os, time
from typing import Any
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR, CURRENCIES, CB_POLICY_RATES, FOREX_PAIRS

CACHE_FILE = os.path.join(CACHE_DIR, "rate_diffs.json")
CACHE_TTL = 7200  # 2 hours (rates don't change frequently)

CB_NAMES = {
    "USD": "Fed", "EUR": "ECB", "GBP": "BoE", "JPY": "BoJ",
    "AUD": "RBA", "NZD": "RBNZ", "CAD": "BoC", "CHF": "SNB",
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

def fetch_rate_differentials() -> dict[str, Any]:
    cached = _load_cache()
    if cached: return cached
    
    rates = {}
    for cur in CURRENCIES:
        rates[cur] = {
            "rate": CB_POLICY_RATES.get(cur, 0),
            "bank": CB_NAMES.get(cur, cur),
        }
    
    # Build differentials matrix
    differentials = {}
    for c1 in CURRENCIES:
        differentials[c1] = {}
        for c2 in CURRENCIES:
            if c1 == c2:
                differentials[c1][c2] = 0
                continue
            diff = round(CB_POLICY_RATES.get(c1, 0) - CB_POLICY_RATES.get(c2, 0), 2)
            differentials[c1][c2] = diff
    
    # Carry trade rankings (highest positive differential)
    carry_pairs = []
    for sym, (base, quote) in FOREX_PAIRS.items():
        if base in CB_POLICY_RATES and quote in CB_POLICY_RATES:
            diff = round(CB_POLICY_RATES[base] - CB_POLICY_RATES[quote], 2)
            carry_pairs.append({
                "pair": sym,
                "base_rate": CB_POLICY_RATES[base],
                "quote_rate": CB_POLICY_RATES[quote],
                "differential": diff,
                "carry_direction": "Buy" if diff > 0 else "Sell" if diff < 0 else "Neutral",
            })
    
    carry_pairs.sort(key=lambda x: abs(x["differential"]), reverse=True)
    
    # Rank currencies by rate
    ranked = sorted(CURRENCIES, key=lambda c: CB_POLICY_RATES.get(c, 0), reverse=True)
    
    result = {
        "rates": rates,
        "differentials": differentials,
        "carry_pairs": carry_pairs,
        "ranking": ranked,
        "currencies": CURRENCIES,
        "source": "Central Bank Official Rates",
        "is_real": True,
    }
    _save_cache(result)
    return result
