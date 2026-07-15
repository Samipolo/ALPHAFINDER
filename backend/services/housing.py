"""Housing Market - official keyless sources:
Freddie Mac PMMS (mortgage rates), FHFA HPI (home prices),
plus live homebuilder / REIT market gauges from Yahoo Finance.
Bloomberg equivalent: USHG
"""
from __future__ import annotations
import csv, io, json, os, time
from typing import Any
import requests
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR

CACHE_FILE = os.path.join(CACHE_DIR, "housing.json")
CACHE_TTL = 7200
UA = {"User-Agent": "Mozilla/5.0"}

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

def _entry(points: list[dict], unit: str) -> dict:
    latest, prev = points[0], points[1] if len(points) > 1 else points[0]
    change = round(latest["value"] - prev["value"], 3)
    return {
        "latest": latest["value"], "date": latest["date"], "prev": prev["value"],
        "change": change,
        "change_pct": round((latest["value"] / prev["value"] - 1) * 100, 2) if prev["value"] else 0,
        "direction": "Rising" if change > 0 else "Falling" if change < 0 else "Flat",
        "unit": unit, "history": points[:12],
    }

def _pmms() -> dict[str, list[dict]]:
    r = requests.get("https://www.freddiemac.com/pmms/docs/PMMS_history.csv",
                     timeout=40, headers=UA)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    m30, m15 = [], []
    for row in rows:
        d = (row.get("date") or "").strip()
        if not d:
            continue
        try:
            m, day, y = d.split("/")
            iso = f"{y}-{int(m):02d}-{int(day):02d}"
        except ValueError:
            continue
        for key, dest in (("pmms30", m30), ("pmms15", m15)):
            v = (row.get(key) or "").strip()
            if v:
                try:
                    dest.append({"date": iso, "value": float(v)})
                except ValueError:
                    pass
    m30.reverse()
    m15.reverse()
    return {"Mortgage_30": m30[:30], "Mortgage_15": m15[:30]}

def _fhfa() -> list[dict]:
    r = requests.get("https://www.fhfa.gov/hpi/download/monthly/hpi_master.csv",
                     timeout=60, headers=UA)
    r.raise_for_status()
    pts = []
    for row in csv.DictReader(io.StringIO(r.text)):
        if (row.get("hpi_type") == "traditional"
                and row.get("hpi_flavor") == "purchase-only"
                and row.get("frequency") == "monthly"
                and (row.get("level") or "").startswith("USA")):
            val = row.get("index_sa") or row.get("index_nsa")
            try:
                pts.append({"date": f"{row['yr']}-{int(row['period']):02d}-01",
                            "value": float(val)})
            except (TypeError, ValueError, KeyError):
                continue
    pts.sort(key=lambda p: p["date"], reverse=True)
    return pts[:24]

def _market_gauges() -> dict[str, list[dict]]:
    import yfinance as yf
    out = {}
    for tkr, name in (("ITB", "Homebuilders_ITB"), ("XHB", "Homebuilders_XHB"), ("VNQ", "REITs_VNQ")):
        try:
            h = yf.Ticker(tkr).history(period="3mo", interval="1d", auto_adjust=True)
            if h is None or h.empty:
                continue
            pts = [{"date": str(idx.date()), "value": round(float(v), 2)}
                   for idx, v in h["Close"].items()]
            pts.reverse()
            out[name] = pts[:30]
        except Exception:
            continue
    return out

def fetch_housing() -> dict[str, Any]:
    cached = _load_cache()
    if cached: return cached

    indicators = {}
    try:
        for name, pts in _pmms().items():
            if pts:
                indicators[name] = _entry(pts, "Percent")
    except Exception:
        pass
    try:
        fhfa = _fhfa()
        if fhfa:
            indicators["FHFA_HPI"] = _entry(fhfa, "Index")
    except Exception:
        pass
    try:
        for name, pts in _market_gauges().items():
            if pts:
                e = _entry(pts, "USD")
                month = pts[min(21, len(pts) - 1)]["value"]
                e["change_pct"] = round((pts[0]["value"] / month - 1) * 100, 2) if month else 0
                e["direction"] = "Rising" if e["change_pct"] > 0 else "Falling" if e["change_pct"] < 0 else "Flat"
                indicators[name] = e
    except Exception:
        pass

    mortgage = indicators.get("Mortgage_30", {}).get("latest", 7)
    itb_dir = indicators.get("Homebuilders_ITB", {}).get("direction", "Flat")
    if mortgage > 7 and itb_dir == "Falling":
        health = "Cooling — High Rates"
    elif mortgage < 5.5 and itb_dir == "Rising":
        health = "Hot — Low Rates"
    elif itb_dir == "Rising":
        health = "Moderate Growth"
    else:
        health = "Stable"

    result = {
        "indicators": indicators,
        "health": health,
        "source": "Freddie Mac PMMS · FHFA HPI · Yahoo Finance",
        "is_real": True,
    }
    if indicators:
        _save_cache(result)
    return result