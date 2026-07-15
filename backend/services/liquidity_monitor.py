"""
Liquidity Monitor - public central bank liquidity gauges from FRED.
Bloomberg inspiration: central bank and liquidity dashboards.
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


CACHE_FILE = os.path.join(CACHE_DIR, "liquidity_monitor.json")
CACHE_TTL = 3600

LIQUIDITY_SERIES = {
    "walcl": {"id": "WALCL", "label": "Fed Balance Sheet", "scale": 0.001, "unit": "$B"},
    "tga": {"id": "WDTGAL", "label": "Treasury General Account", "scale": 0.001, "unit": "$B"},
    "rrp": {"id": "RRPONTSYD", "label": "Reverse Repo Facility", "scale": 1.0, "unit": "$B"},
    "reserves": {"id": "TOTRESNS", "label": "Bank Reserves", "scale": 0.001, "unit": "$B"},
    "sofr": {"id": "SOFR", "label": "SOFR", "scale": 1.0, "unit": "%"},
    "fed_funds": {"id": "DFF", "label": "Fed Funds Effective", "scale": 1.0, "unit": "%"},
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


def _get_fred_series(session, series_id: str, limit: int = 60) -> list[dict[str, Any]]:
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


def _scaled(value: float, scale: float) -> float:
    return round(value * scale, 3)


def _record_from_series(meta: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    latest = _scaled(rows[0]["value"], meta["scale"])
    prev = _scaled(rows[1]["value"], meta["scale"]) if len(rows) > 1 else latest
    week = _scaled(rows[4]["value"], meta["scale"]) if len(rows) > 4 else prev
    return {
        "label": meta["label"],
        "series_id": meta["id"],
        "latest": latest,
        "d1": round(latest - prev, 3),
        "w1": round(latest - week, 3),
        "date": rows[0]["date"],
        "unit": meta["unit"],
        "history": [{"date": row["date"], "value": _scaled(row["value"], meta["scale"])} for row in rows[:24]],
    }


def fetch_liquidity_monitor() -> dict[str, Any]:
    cached = _load_cache()
    if cached:
        return cached

    session = build_session()
    series: dict[str, dict[str, Any]] = {}
    for key, meta in LIQUIDITY_SERIES.items():
        try:
            rows = _get_fred_series(session, meta["id"])
        except Exception:
            continue
        record = _record_from_series(meta, rows)
        if record:
            series[key] = record

    walcl = (series.get("walcl") or {}).get("latest")
    tga = (series.get("tga") or {}).get("latest")
    rrp = (series.get("rrp") or {}).get("latest")
    net_liquidity = None
    if walcl is not None and tga is not None and rrp is not None:
        net_liquidity = round(walcl - tga - rrp, 3)

    walcl_w1 = (series.get("walcl") or {}).get("w1") or 0.0
    tga_w1 = (series.get("tga") or {}).get("w1") or 0.0
    rrp_w1 = (series.get("rrp") or {}).get("w1") or 0.0
    net_liquidity_w1 = round(walcl_w1 - tga_w1 - rrp_w1, 3)

    if net_liquidity_w1 >= 75:
        regime = "Liquidity Expanding"
    elif net_liquidity_w1 <= -75:
        regime = "Liquidity Draining"
    else:
        regime = "Liquidity Stable"

    result = {
        "series": series,
        "net_liquidity": net_liquidity,
        "net_liquidity_w1": net_liquidity_w1,
        "regime": regime,
        "source": "FRED / Federal Reserve balance-sheet and rates data",
        "is_real": True,
    }
    _save_cache(result)
    return result
