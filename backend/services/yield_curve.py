"""Yield Curve Lab - official U.S. Treasury daily par yield curve (no API key).
Bloomberg equivalent: GC (Government Curve)
"""
from __future__ import annotations
import csv, io, json, os, time
from datetime import date
from typing import Any
import requests
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR

CACHE_FILE = os.path.join(CACHE_DIR, "yield_curve.json")
CACHE_TTL = 3600
TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/"
                "interest-rates/daily-treasury-rates.csv/{year}/all"
                "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")
TENOR_MAP = {"1 Mo": "1M", "2 Mo": "2M", "3 Mo": "3M", "4 Mo": "4M", "6 Mo": "6M",
             "1 Yr": "1Y", "2 Yr": "2Y", "3 Yr": "3Y", "5 Yr": "5Y", "7 Yr": "7Y",
             "10 Yr": "10Y", "20 Yr": "20Y", "30 Yr": "30Y"}

def _load_cache() -> dict | None:
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

def _fetch_year(year: int) -> list[dict]:
    resp = requests.get(TREASURY_URL.format(year=year), timeout=30,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return list(csv.DictReader(io.StringIO(resp.text)))

def fetch_yield_curve() -> dict[str, Any]:
    cached = _load_cache()
    if cached: return cached

    year = date.today().year
    rows = _fetch_year(year)
    if len(rows) < 45:
        try:
            rows += _fetch_year(year - 1)
        except Exception:
            pass

    def _key(r):
        m, d, y = r["Date"].split("/")
        return (int(y), int(m), int(d))
    rows.sort(key=_key, reverse=True)

    history: dict[str, list] = {t: [] for t in TENOR_MAP.values()}
    for r in rows[:60]:
        m, d, y = r["Date"].split("/")
        iso = f"{y}-{int(m):02d}-{int(d):02d}"
        for col, tenor in TENOR_MAP.items():
            v = (r.get(col) or "").strip()
            if v and v != "N/A":
                try:
                    history[tenor].append({"date": iso, "value": float(v)})
                except ValueError:
                    pass
    history = {t: h for t, h in history.items() if h}
    latest_curve = {t: h[0]["value"] for t, h in history.items()}

    y10 = latest_curve.get("10Y")
    y2 = latest_curve.get("2Y")
    y3m = latest_curve.get("3M")
    spread_10_2 = round(y10 - y2, 3) if y10 is not None and y2 is not None else None
    spread_10_3m = round(y10 - y3m, 3) if y10 is not None and y3m is not None else None
    inverted = spread_10_2 is not None and spread_10_2 < 0

    if inverted:
        regime = "Inverted — Recession Signal"
    elif spread_10_2 is not None and spread_10_2 < 0.25:
        regime = "Flat — Uncertainty"
    elif spread_10_2 is not None and spread_10_2 > 1.5:
        regime = "Steep — Growth Expectation"
    elif spread_10_2 is not None:
        regime = "Normal — Healthy"
    else:
        regime = "Unknown"

    result = {
        "curve": latest_curve,
        "spread_10_2": spread_10_2,
        "spread_10_3m": spread_10_3m,
        "inverted": inverted,
        "regime": regime,
        "history": history,
        "source": "U.S. Treasury — official daily par yield curve",
        "is_real": True,
    }
    if latest_curve:
        _save_cache(result)
    return result