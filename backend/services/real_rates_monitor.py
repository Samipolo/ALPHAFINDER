"""
Real Rates Monitor - public Treasury real yield and breakeven inflation data from FRED.
Bloomberg inspiration: real-yield and inflation expectation monitors.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR, FRED_BASE, FRED_KEY  # noqa: E402
from services.net_utils import build_session  # noqa: E402


CACHE_FILE = os.path.join(CACHE_DIR, "real_rates_monitor.json")
CACHE_TTL = 3600

REAL_RATE_SERIES = {
    "nominal_10y": {"id": "DGS10", "label": "10Y Nominal", "unit": "%"},
    "real_10y": {"id": "DFII10", "label": "10Y Real Yield", "unit": "%"},
    "breakeven_10y": {"id": "T10YIE", "label": "10Y Breakeven", "unit": "%"},
    "nominal_5y": {"id": "DGS5", "label": "5Y Nominal", "unit": "%"},
    "breakeven_5y": {"id": "T5YIE", "label": "5Y Breakeven", "unit": "%"},
}


def _load_cache() -> dict[str, Any] | None:
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        if time.time() - os.path.getmtime(CACHE_FILE) > CACHE_TTL:
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle).get("data")
    except Exception:
        return None


def _save_cache(data: dict[str, Any]) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(
                {"as_of": datetime.now(timezone.utc).isoformat(), "data": data},
                handle,
            )
    except Exception:
        pass


def _get_fred_series(session, series_id: str, limit: int = 90) -> list[dict[str, Any]]:
    resp = session.get(
        FRED_BASE,
        params={
            "series_id": series_id,
            "api_key": FRED_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        },
        timeout=20,
    )
    resp.raise_for_status()
    observations = resp.json().get("observations", [])
    rows: list[dict[str, Any]] = []
    for item in observations:
        raw = item.get("value")
        if raw in (None, "", "."):
            continue
        try:
            rows.append({"date": item["date"], "value": float(raw)})
        except (TypeError, ValueError):
            continue
    return rows


def _record_from_series(meta: dict[str, str], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    latest = rows[0]["value"]
    prev = rows[1]["value"] if len(rows) > 1 else latest
    week = rows[5]["value"] if len(rows) > 5 else prev
    return {
        "label": meta["label"],
        "series_id": meta["id"],
        "latest": round(latest, 3),
        "d1": round(latest - prev, 3),
        "w1": round(latest - week, 3),
        "date": rows[0]["date"],
        "unit": meta["unit"],
        "history": rows[:30],
    }


def fetch_real_rates_monitor() -> dict[str, Any]:
    cached = _load_cache()
    if cached:
        return cached

    session = build_session()
    series: dict[str, dict[str, Any]] = {}
    for key, meta in REAL_RATE_SERIES.items():
        try:
            rows = _get_fred_series(session, meta["id"])
        except Exception:
            continue
        record = _record_from_series(meta, rows)
        if record:
            series[key] = record

    real_10y = (series.get("real_10y") or {}).get("latest")
    real_10y_w1 = (series.get("real_10y") or {}).get("w1")
    be_10y = (series.get("breakeven_10y") or {}).get("latest")
    be_5y = (series.get("breakeven_5y") or {}).get("latest")

    if real_10y is not None and real_10y >= 1.75 and (real_10y_w1 or 0) > 0:
        regime = "Restrictive real-yield backdrop"
    elif real_10y is not None and real_10y <= 1.0:
        regime = "Easy real-yield backdrop"
    else:
        regime = "Balanced real-yield backdrop"

    breakeven_curve = round(be_10y - be_5y, 3) if be_10y is not None and be_5y is not None else None

    result = {
        "series": series,
        "regime": regime,
        "breakeven_curve": breakeven_curve,
        "source": "FRED / Treasury real yields and breakeven inflation",
        "is_real": True,
    }
    _save_cache(result)
    return result
