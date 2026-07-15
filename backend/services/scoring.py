"""
Scoring Engine — A1 Trading AlphaFinder Methodology.
Combines five scored pillars into a single bias score per asset:
  1. Trend        (−3 to +3)  — SMA3/SMA14 crossover + slope
  2. Seasonality  (−2 or +2)  — 10-year monthly average sign
  3. COT          (−2 to +2)  — Weekly % change in institutional positioning
  4. Retail       (−2 to +2)  — 60% contrarian threshold
  5. Macro        (variable)  — Growth + Inflation + Labour surprise vs forecast

Bias thresholds:
  ≥ +9  Very Bullish
  ≥ +5  Bullish
  −4…+4 Neutral
  ≤ −5  Bearish
  ≤ −9  Very Bearish
"""
from __future__ import annotations
from typing import List, Dict, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CURRENCIES, ECON_FIELDS, FOREX_PAIRS, INDICES, COMMODITIES, CRYPTO, DXY


def _clamp(v: int, lo: int = -3, hi: int = 3) -> int:
    """Clamp value to range. Default −3 to +3 for A1 trend scores."""
    return max(lo, min(hi, v))


# ── Economic field groups (A1 categories) ───────────────────────────────
FIELD_GROUPS = {
    "growth": ["GDP", "mPMI", "sPMI", "Retail Sales", "Consumer Conf"],
    "inflation": ["CPI", "PPI", "PCE", "Interest Rates"],
    "jobs": ["NFP", "Unemployment Rate", "Unemployment Claims", "ADP"],
}


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── COT Scoring (Pillar 3) ──────────────────────────────────────────────
def _cot_forex(base: str, quote: str, cot_map: dict) -> int:
    """
    A1 COT score for forex pairs.
    Takes the weekly % change in institutional net-long positioning
    for the base currency minus the quote currency.
    """
    bc = cot_map.get(base)
    qc = cot_map.get(quote)
    if not bc or not qc:
        return 0
    try:
        base_change = float(bc.get("net_pct_change_raw", 0))
        quote_change = float(qc.get("net_pct_change_raw", 0))
        diff = base_change - quote_change
    except (ValueError, TypeError):
        return 0

    if diff > 5:     return 2   # Strongly bullish — large speculators accumulating base
    if diff > 0:     return 1   # Mildly bullish
    if diff < -5:    return -2  # Strongly bearish — distributing base
    if diff < 0:     return -1  # Mildly bearish
    return 0


def _cot_direct(sym: str, cot_map: dict) -> int:
    """
    A1 COT score for indices/commodities/crypto.
    Uses the weekly % change in net-long positioning for the asset directly.
    """
    idx_map = {
        "SPX500": "SPX",   "NAS100": "NASDAQ", "US30": "DOW",
        "XAUUSD": "Gold",  "XAGUSD": "Silver",  "USOIL": "USOil",
        "BTCUSD": "BTC",   "DXY":    "USD",
        "UK100":  "GBP",   "GER40":  "EUR",     "JP225": "JPY",
    }
    cot_sym = idx_map.get(sym)
    if not cot_sym or cot_sym not in cot_map:
        return 0
    try:
        record = cot_map[cot_sym]
        change = float(record.get("net_pct_change_raw", 0))
    except (ValueError, TypeError):
        return 0

    if change > 5:     return 2
    if change > 0:     return 1
    if change < -5:    return -2
    if change < 0:     return -1
    return 0


# ── Retail Scoring (Pillar 4) — 60% Contrarian Threshold ───────────────
def _retail_direction_from_long_pct(lp: float) -> int:
    """
    A1 Trading contrarian logic: retail traders are systematically
    wrong at extremes, so the tool fades them.
      60%+ long  → −2 (bearish contrarian)
      60%+ short → +2 (bullish contrarian)
      Below 60%  → 0  (no signal)
    """
    if lp >= 60:  return -2   # 60%+ retail long → fade them → bearish
    if lp >= 55:  return -1   # mild crowding long → mildly bearish
    if lp <= 40:  return 2    # 60%+ retail short (long% ≤ 40) → fade → bullish
    if lp <= 45:  return 1    # mild crowding short → mildly bullish
    return 0


def _retail_leg_score(sym: str, retail_map: dict) -> int:
    r = retail_map.get(sym)
    if not r:
        return 0
    try:
        lp = float(r.get("long_pct", 50))
    except (ValueError, TypeError):
        return 0
    return _retail_direction_from_long_pct(lp)


def _retail_usd_proxy(currency: str, retail_map: dict) -> int:
    """Get retail signal for a currency via its USD pair."""
    if currency == "USD":
        return 0
    direct = currency + "USD"
    inverse = "USD" + currency
    if direct in retail_map:
        return _retail_leg_score(direct, retail_map)
    if inverse in retail_map:
        return -_retail_leg_score(inverse, retail_map)
    return 0


def _retail_score(sym: str, retail_map: dict) -> int:
    """
    A1 retail score for a pair/asset.
    Direct pair lookup first, then synthesize from currency proxies.
    """
    # Try direct pair lookup
    direct = _retail_leg_score(sym, retail_map)
    if direct != 0 or sym in retail_map:
        return direct

    # DXY: inverse basket of majors' retail positioning
    if sym == "DXY":
        legs = [_retail_usd_proxy(c, retail_map)
                for c in ("EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF")]
        active = [v for v in legs if v != 0]
        if not active:
            return 0
        avg = -sum(active) / len(active)
        if avg >= 1.5:   return 2
        if avg >= 0.5:   return 1
        if avg <= -1.5:  return -2
        if avg <= -0.5:  return -1
        return 0

    # For forex pairs, synthesize from base/quote currencies
    if len(sym) != 6:
        return 0
    base = sym[:3]
    quote = sym[3:]
    base_score = _retail_usd_proxy(base, retail_map)
    quote_score = _retail_usd_proxy(quote, retail_map)
    diff = base_score - quote_score
    if diff >= 2:    return 2
    if diff > 0:     return 1
    if diff <= -2:   return -2
    if diff < 0:     return -1
    return 0


# ── Macro Scoring (Pillar 5) ────────────────────────────────────────────
def _group_signal(values: list[Optional[int]]) -> int:
    """
    Aggregate a group of economic field scores into a single group signal.
    Uses the average of available scores.
    """
    clean = [int(v) for v in values if v is not None]
    if not clean:
        return 0
    avg = sum(clean) / len(clean)
    if avg >= 1.2:   return 2
    if avg >= 0.35:  return 1
    if avg <= -1.2:  return -2
    if avg <= -0.35: return -1
    return 0


def _merge_field_sources(base_source: Optional[str], quote_source: Optional[str]) -> str:
    left = (base_source or "missing").lower()
    right = (quote_source or "missing").lower()
    if "weekly" in (left, right):
        return "weekly"
    if left == "monthly" or right == "monthly":
        return "monthly"
    if left == "mixed" or right == "mixed":
        return "mixed"
    return "missing"


def _single_field_source(source: Optional[str]) -> str:
    value = (source or "missing").lower()
    if "weekly" in value:
        return "weekly"
    if value == "monthly":
        return "monthly"
    if value == "mixed":
        return "mixed"
    return "missing"


# ── Master Setup Calculator ─────────────────────────────────────────────
def calculate_setups(tech: dict, econ: dict, cot_list: list, retail_list: list) -> list:
    """
    A1 Trading AlphaFinder — compute bias score for every tracked asset.

    Five pillars summed:
      1. Trend       (−3 to +3)  from technical.py SMA3/14
      2. Seasonality (−2 or +2)  from technical.py monthly avgs
      3. COT         (−2 to +2)  weekly institutional positioning change
      4. Retail      (−2 to +2)  60% contrarian threshold
      5. Macro       (variable)  growth + inflation + labour surprise

    Bias thresholds: ≥9 Very Bullish, ≥5 Bullish, −4…+4 Neutral,
                     ≤−5 Bearish, ≤−9 Very Bearish
    """
    cot_map    = {c.get("symbol", ""): c for c in cot_list}
    retail_map = {r.get("symbol", ""): r for r in retail_list}

    # Build all asset entries
    all_assets = []
    for sym, (base, quote) in FOREX_PAIRS.items():
        all_assets.append({"sym": sym, "type": "forex", "base": base, "quote": quote})
    for sym, (tkr, cur) in INDICES.items():
        all_assets.append({"sym": sym, "type": "index", "base": cur, "quote": None})
    for sym, (tkr, cur) in COMMODITIES.items():
        all_assets.append({"sym": sym, "type": "commodity", "base": cur, "quote": None})
    for sym, (tkr, cur) in CRYPTO.items():
        all_assets.append({"sym": sym, "type": "crypto", "base": cur, "quote": None})
    for sym, (tkr, cur) in DXY.items():
        all_assets.append({"sym": sym, "type": "dxy", "base": cur, "quote": None})

    results = []

    for asset in all_assets:
        sym   = asset["sym"]
        atype = asset["type"]
        base  = asset["base"]
        quote = asset.get("quote")

        score = 0
        det = {
            "symbol":      sym,
            "trend":       0,
            "seasonality": 0,
            "cot":         0,
            "retail":      0,
            "macro":       0,
            "bias":        "Neutral",
            "econ":        {f: None for f in ECON_FIELDS},
            "raw_econ":    {f: None for f in ECON_FIELDS},
            "econ_sources": {f: "missing" for f in ECON_FIELDS},
            "econ_missing": [],
            "econ_inferred": [],
            "price":       None,
            "sma3":        None,
            "sma14":       None,
            "sma20":       None,
            "sma50":       None,
            "sma200":      None,
            "macro_profile": {},
            "components": {},
        }

        # ── Pillar 1: Technical Trend (−3 to +3) ──
        td = tech.get(sym, {})
        det["trend"]       = _safe_int(td.get("trend", 0))
        det["trend"]       = max(-3, min(3, det["trend"]))  # Ensure A1 range

        # ── Pillar 2: Seasonality (−2 or +2) ──
        det["seasonality"] = _safe_int(td.get("seasonality", 0))

        # Store price data for display
        det["price"]  = td.get("price")
        det["sma3"]   = td.get("sma3")
        det["sma14"]  = td.get("sma14")
        det["sma20"]  = td.get("sma20")
        det["sma50"]  = td.get("sma50")
        det["sma200"] = td.get("sma200")

        score += det["trend"] + det["seasonality"]

        # ── Pillar 3: COT (−2 to +2) ──
        if atype == "forex":
            cot_score = _cot_forex(base, quote, cot_map)
        else:
            cot_score = _cot_direct(sym, cot_map)
        det["cot"] = cot_score
        score += cot_score

        # ── Pillar 4: Retail Sentiment (−2 to +2, contrarian) ──
        if atype == "forex":
            rs = _retail_score(sym, retail_map)
        else:
            rs = 0  # Retail data only available for forex
        det["retail"] = rs
        score += rs

        # ── Pillar 5: Macro / Economic Score ──
        growth_signal = 0
        inflation_signal = 0
        jobs_signal = 0

        if atype == "forex" and base in CURRENCIES and quote in CURRENCIES:
            be = econ.get(base, {})
            qe = econ.get(quote, {})
            base_sources = be.get("_field_sources", {})
            quote_sources = qe.get("_field_sources", {})
            for f in ECON_FIELDS:
                base_v = be.get(f)
                quote_v = qe.get(f)
                det["econ_sources"][f] = _merge_field_sources(
                    base_sources.get(f), quote_sources.get(f)
                )
                if base_v is None and quote_v is None:
                    det["raw_econ"][f] = None
                    det["econ"][f] = None
                    det["econ_missing"].append(f)
                    continue
                raw_v = _clamp(_safe_int(base_v) - _safe_int(quote_v), -2, 2)
                det["raw_econ"][f] = raw_v
                det["econ"][f] = raw_v

            growth_signal = _group_signal([det["econ"][field] for field in FIELD_GROUPS["growth"]])
            inflation_signal = _group_signal([det["econ"][field] for field in FIELD_GROUPS["inflation"]])
            jobs_signal = _group_signal([det["econ"][field] for field in FIELD_GROUPS["jobs"]])
            macro_signal = growth_signal + inflation_signal + jobs_signal
            det["macro"] = macro_signal
            score += macro_signal

        elif atype == "index":
            idx_cur = {
                "SPX500": "USD", "NAS100": "USD", "US30": "USD",
                "UK100":  "GBP", "GER40":  "EUR", "JP225": "JPY",
            }.get(sym, "USD")
            ce = econ.get(idx_cur, {})
            field_sources = ce.get("_field_sources", {})
            for f in ECON_FIELDS:
                value = ce.get(f)
                det["econ_sources"][f] = _single_field_source(field_sources.get(f))
                if value is None:
                    det["raw_econ"][f] = None
                    det["econ"][f] = None
                    det["econ_missing"].append(f)
                    continue
                raw_v = _safe_int(value)
                det["raw_econ"][f] = raw_v
                det["econ"][f] = raw_v

            growth_signal = _group_signal([det["econ"][field] for field in FIELD_GROUPS["growth"]])
            inflation_signal = _group_signal([det["econ"][field] for field in FIELD_GROUPS["inflation"]])
            jobs_signal = _group_signal([det["econ"][field] for field in FIELD_GROUPS["jobs"]])
            macro_signal = growth_signal + inflation_signal + jobs_signal
            det["macro"] = macro_signal
            score += macro_signal

        elif atype in ("commodity", "crypto"):
            # Commodities/crypto are priced in USD — invert USD strength
            ue = econ.get("USD", {})
            field_sources = ue.get("_field_sources", {})
            for f in ECON_FIELDS:
                value = ue.get(f)
                det["econ_sources"][f] = _single_field_source(field_sources.get(f))
                if value is None:
                    det["raw_econ"][f] = None
                    det["econ"][f] = None
                    det["econ_missing"].append(f)
                    continue
                raw_v = -_safe_int(value)  # Invert: strong USD = bearish for commodities
                det["raw_econ"][f] = raw_v
                det["econ"][f] = raw_v

            growth_signal = _group_signal([det["econ"][field] for field in FIELD_GROUPS["growth"]])
            inflation_signal = _group_signal([det["econ"][field] for field in FIELD_GROUPS["inflation"]])
            jobs_signal = _group_signal([det["econ"][field] for field in FIELD_GROUPS["jobs"]])
            macro_signal = growth_signal + inflation_signal + jobs_signal
            det["macro"] = macro_signal
            score += macro_signal

        elif atype == "dxy":
            ue = econ.get("USD", {})
            field_sources = ue.get("_field_sources", {})
            for f in ECON_FIELDS:
                value = ue.get(f)
                det["econ_sources"][f] = _single_field_source(field_sources.get(f))
                if value is None:
                    det["raw_econ"][f] = None
                    det["econ"][f] = None
                    det["econ_missing"].append(f)
                    continue
                raw_v = _safe_int(value)
                det["raw_econ"][f] = raw_v
                det["econ"][f] = raw_v

            growth_signal = _group_signal([det["econ"][field] for field in FIELD_GROUPS["growth"]])
            inflation_signal = _group_signal([det["econ"][field] for field in FIELD_GROUPS["inflation"]])
            jobs_signal = _group_signal([det["econ"][field] for field in FIELD_GROUPS["jobs"]])
            macro_signal = growth_signal + inflation_signal + jobs_signal
            det["macro"] = macro_signal
            score += macro_signal

        # ── Component breakdown ──
        det["components"] = {
            "trend": det["trend"],
            "seasonality": det["seasonality"],
            "cot": det["cot"],
            "retail": det["retail"],
            "macro": det["macro"],
            "growth": growth_signal,
            "inflation": inflation_signal,
            "jobs": jobs_signal,
        }

        # ── A1 Trading Bias Thresholds ──
        det["total_score"] = score
        if score >= 9:       det["bias"] = "Very Bullish"
        elif score >= 5:     det["bias"] = "Bullish"
        elif score <= -9:    det["bias"] = "Very Bearish"
        elif score <= -5:    det["bias"] = "Bearish"
        else:                det["bias"] = "Neutral"

        results.append(det)

    results.sort(key=lambda x: x["total_score"], reverse=True)
    return results
