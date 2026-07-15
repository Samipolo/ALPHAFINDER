"""Global Macro Dashboard - official keyless sources:
BLS public API (CPI, jobs), NY Fed (EFFR), World Bank (GDP growth).
Bloomberg equivalent: ECST
"""
from __future__ import annotations
import json, os, time
from typing import Any
import requests
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR

CACHE_FILE = os.path.join(CACHE_DIR, "global_macro.json")
CACHE_TTL = 3600
UA = {"User-Agent": "Mozilla/5.0"}

BLS_SERIES = {
    "CUUR0000SA0": "CPI",
    "CUUR0000SA0L1E": "Core_CPI",
    "LNS14000000": "Unemployment",
    "CES0000000001": "NFP",
    "CES0500000003": "Hourly_Earnings",
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

def _indicator(points: list[dict], yoy_lookback: int | None = 12,
               level_change: bool = False) -> dict:
    """points newest-first [{date, value}]."""
    latest, prev = points[0], points[1] if len(points) > 1 else points[0]
    change = round(latest["value"] - prev["value"], 3)
    yoy = None
    if yoy_lookback and len(points) > yoy_lookback and not level_change:
        base = points[yoy_lookback]["value"]
        if base:
            yoy = round((latest["value"] / base - 1) * 100, 2)
    return {
        "latest": latest["value"],
        "date": latest["date"],
        "prev": prev["value"],
        "change": change,
        "yoy_pct": yoy,
        "direction": "Rising" if change > 0 else "Falling" if change < 0 else "Flat",
        "history": points[:12],
    }

def _bls() -> dict[str, list[dict]]:
    r = requests.post("https://api.bls.gov/publicAPI/v1/timeseries/data/",
                      json={"seriesid": list(BLS_SERIES)}, timeout=30, headers=UA)
    r.raise_for_status()
    out = {}
    for s in r.json().get("Results", {}).get("series", []):
        name = BLS_SERIES.get(s.get("seriesID"))
        pts = []
        for item in s.get("data", []):
            period = item.get("period", "")
            if not period.startswith("M"):
                continue
            try:
                pts.append({"date": f"{item['year']}-{int(period[1:]):02d}-01",
                            "value": float(item["value"])})
            except (ValueError, KeyError):
                continue
        if name and pts:
            out[name] = pts
    return out

def _effr() -> list[dict]:
    r = requests.get("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/30.json",
                     timeout=25, headers=UA)
    r.raise_for_status()
    return [{"date": row.get("effectiveDate"), "value": float(row.get("percentRate"))}
            for row in r.json().get("refRates", []) if row.get("percentRate") is not None]

def _worldbank_gdp() -> list[dict]:
    url = ("https://api.worldbank.org/v2/country/USA/indicator/"
           "NY.GDP.MKTP.KD.ZG?format=json&per_page=12")
    r = requests.get(url, timeout=25, headers=UA)
    r.raise_for_status()
    body = r.json()
    rows = body[1] if len(body) > 1 and body[1] else []
    return [{"date": f"{row['date']}-12-31", "value": round(float(row["value"]), 2)}
            for row in rows if row.get("value") is not None]

def fetch_global_macro() -> dict[str, Any]:
    cached = _load_cache()
    if cached: return cached

    indicators = {}
    try:
        for name, pts in _bls().items():
            indicators[name] = _indicator(
                pts, level_change=name == "Unemployment")
    except Exception:
        pass
    try:
        effr = _effr()
        if effr:
            indicators["Fed_Funds"] = _indicator(effr, yoy_lookback=None, level_change=True)
    except Exception:
        pass
    try:
        gdp = _worldbank_gdp()
        if gdp:
            ind = _indicator(gdp, yoy_lookback=None, level_change=True)
            ind["yoy_pct"] = None
            indicators["GDP_Growth"] = ind
    except Exception:
        pass

    cpi_yoy = indicators.get("CPI", {}).get("yoy_pct")
    unemp = indicators.get("Unemployment", {}).get("latest", 4)
    gdp_g = indicators.get("GDP_Growth", {}).get("latest", 2)
    if isinstance(gdp_g, (int, float)) and gdp_g > 2 and unemp < 4.5:
        health = "Expansion"
    elif isinstance(gdp_g, (int, float)) and gdp_g < 0.5:
        health = "Contraction"
    elif isinstance(cpi_yoy, (int, float)) and cpi_yoy > 3.5:
        health = "Inflationary"
    else:
        health = "Mixed"

    result = {
        "indicators": indicators,
        "health": health,
        "source": "BLS · NY Fed · World Bank (official APIs)",
        "is_real": True,
    }
    if indicators:
        _save_cache(result)
    return result