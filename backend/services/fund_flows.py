"""Fund Flows / Liquidity - official keyless sources:
NY Fed SOMA (Fed balance sheet), Treasury FiscalData (TGA, debt),
NY Fed reverse repo, DBnomics FED H.6 (M2).
Bloomberg equivalent: FLOW
"""
from __future__ import annotations
import json, os, time
from datetime import date, timedelta
from typing import Any
import requests
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR

CACHE_FILE = os.path.join(CACHE_DIR, "fund_flows.json")
CACHE_TTL = 3600
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

def _mk_series(points: list[dict], idx_w: int, idx_m: int) -> dict | None:
    """points newest-first [{date, value}] in billions USD."""
    if not points:
        return None
    latest = points[0]["value"]
    prev = points[min(idx_w, len(points) - 1)]["value"]
    month = points[min(idx_m, len(points) - 1)]["value"]
    return {
        "latest": round(latest, 2),
        "prev": round(prev, 2),
        "wow": round((latest / prev - 1) * 100, 3) if prev else 0,
        "mom": round((latest / month - 1) * 100, 3) if month else 0,
        "history": points[:24],
        "unit": "Billions USD",
    }

def _soma() -> list[dict]:
    r = requests.get("https://markets.newyorkfed.org/api/soma/summary.json", timeout=30, headers=UA)
    r.raise_for_status()
    rows = r.json().get("soma", {}).get("summary", [])
    out = []
    for row in rows[-70:]:
        total = row.get("total")
        if total in (None, ""):
            total = sum(float(row.get(k) or 0) for k in
                        ("mbs", "cmbs", "tips", "frn", "tipsInflationCompensation",
                         "notesbonds", "bills", "agencies"))
        out.append({"date": row.get("asOfDate"), "value": float(total) / 1e9})
    out.reverse()
    return out

def _tga() -> list[dict]:
    url = ("https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/"
           "accounting/dts/operating_cash_balance?sort=-record_date&page[size]=300")
    r = requests.get(url, timeout=30, headers=UA)
    r.raise_for_status()
    out = []
    for row in r.json().get("data", []):
        if "Closing Balance" in (row.get("account_type") or "") and "TGA" in (row.get("account_type") or ""):
            bal = row.get("open_today_bal")
            if bal not in (None, "", "null"):
                out.append({"date": row["record_date"], "value": float(bal) / 1000.0})
    return out[:60]

def _rrp() -> list[dict]:
    start = (date.today() - timedelta(days=100)).isoformat()
    url = f"https://markets.newyorkfed.org/api/rp/reverserepo/propositions/search.json?startDate={start}"
    r = requests.get(url, timeout=30, headers=UA)
    r.raise_for_status()
    ops = r.json().get("repo", {}).get("operations", [])
    pts = [{"date": o.get("operationDate"), "value": float(o.get("totalAmtAccepted") or 0) / 1e9}
           for o in ops if o.get("operationDate")]
    pts.sort(key=lambda p: p["date"], reverse=True)
    return pts[:60]

def _debt() -> list[dict]:
    url = ("https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/"
           "accounting/od/debt_to_penny?sort=-record_date&page[size]=60")
    r = requests.get(url, timeout=30, headers=UA)
    r.raise_for_status()
    return [{"date": row["record_date"], "value": float(row["tot_pub_debt_out_amt"]) / 1e9}
            for row in r.json().get("data", []) if row.get("tot_pub_debt_out_amt")]

def _m2() -> list[dict]:
    url = "https://api.db.nomics.world/v22/series/FED/H6_H6_M2?limit=30&observations=1"
    r = requests.get(url, timeout=30, headers=UA)
    r.raise_for_status()
    docs = r.json().get("series", {}).get("docs", [])
    best = None
    for doc in docs:
        name = (doc.get("series_name") or "").lower()
        if "m2" in name and "seasonally adjusted" in name and "not seasonally" not in name:
            best = doc
            break
    if best is None and docs:
        best = docs[0]
    if not best:
        return []
    periods = best.get("period", [])
    values = best.get("value", [])
    pts = [{"date": p, "value": float(v)} for p, v in zip(periods, values)
           if isinstance(v, (int, float))]
    pts.reverse()
    return pts[:24]

def fetch_fund_flows() -> dict[str, Any]:
    cached = _load_cache()
    if cached: return cached

    series = {}
    fetchers = {
        "Fed_Balance": (_soma, 1, 4),
        "TGA": (_tga, 5, 21),
        "Reverse_Repo": (_rrp, 5, 21),
        "M2": (_m2, 1, 1),
        "Debt_Outstanding": (_debt, 5, 21),
    }
    for name, (fn, iw, im) in fetchers.items():
        try:
            s = _mk_series(fn(), iw, im)
            if s:
                series[name] = s
        except Exception:
            continue

    # Net liquidity = Fed balance sheet - TGA - RRP (the trader's liquidity proxy)
    fb, tga, rrp = series.get("Fed_Balance"), series.get("TGA"), series.get("Reverse_Repo")
    if fb and tga and rrp:
        latest = fb["latest"] - tga["latest"] - rrp["latest"]
        prev = fb["prev"] - tga["prev"] - rrp["prev"]
        series["Net_Liquidity"] = {
            "latest": round(latest, 2), "prev": round(prev, 2),
            "wow": round((latest / prev - 1) * 100, 3) if prev else 0,
            "mom": round((latest / prev - 1) * 100, 3) if prev else 0,
            "history": [], "unit": "Billions USD",
        }

    net_mom = series.get("Net_Liquidity", {}).get("wow", 0)
    fed_mom = series.get("Fed_Balance", {}).get("mom", 0)
    if net_mom > 0.5 or (net_mom > 0 and fed_mom > 0):
        liquidity, signal = "Expanding — Risk On", 1
    elif net_mom < -0.5 or fed_mom < -1:
        liquidity, signal = "Tightening — Risk Off", -1
    else:
        liquidity, signal = "Stable", 0

    result = {
        "series": series,
        "liquidity_regime": liquidity,
        "liquidity_signal": signal,
        "source": "NY Fed SOMA · Treasury FiscalData · Fed H.6",
        "is_real": True,
    }
    if series:
        _save_cache(result)
    return result