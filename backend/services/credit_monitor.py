"""
Credit Monitor - public spread and yield dashboards from FRED.
Bloomberg inspiration: fixed income monitors and credit spread dashboards.
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


CACHE_FILE = os.path.join(CACHE_DIR, "credit_monitor.json")
CACHE_TTL = 3600

CREDIT_SERIES = {
    "ig_oas": {"id": "BAMLC0A0CM", "label": "Investment Grade OAS", "unit": "%"},
    "hy_oas": {"id": "BAMLH0A0HYM2", "label": "High Yield OAS", "unit": "%"},
    "ig_yield": {"id": "BAMLC0A0CMEY", "label": "Investment Grade Yield", "unit": "%"},
    "hy_yield": {"id": "BAMLH0A0HYM2EY", "label": "High Yield Yield", "unit": "%"},
    "baa_10y": {"id": "BAA10Y", "label": "Baa minus 10Y Treasury", "unit": "%"},
    "real_10y": {"id": "DFII10", "label": "10Y Real Yield", "unit": "%"},
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
    month = rows[21]["value"] if len(rows) > 21 else week
    return {
        "label": meta["label"],
        "series_id": meta["id"],
        "latest": round(latest, 3),
        "d1": round(latest - prev, 3),
        "w1": round(latest - week, 3),
        "m1": round(latest - month, 3),
        "date": rows[0]["date"],
        "unit": meta["unit"],
        "history": rows[:30],
    }


def _credit_regime(series: dict[str, dict[str, Any]]) -> tuple[str, int]:
    hy_oas = (series.get("hy_oas") or {}).get("latest")
    ig_oas = (series.get("ig_oas") or {}).get("latest")
    baa = (series.get("baa_10y") or {}).get("latest")
    hy_w1 = (series.get("hy_oas") or {}).get("w1")

    score = 0
    if hy_oas is not None:
        if hy_oas <= 3.5:
            score += 2
        elif hy_oas >= 4.75:
            score -= 2
    if ig_oas is not None:
        if ig_oas <= 1.2:
            score += 1
        elif ig_oas >= 1.75:
            score -= 1
    if baa is not None:
        if baa <= 1.7:
            score += 1
        elif baa >= 2.1:
            score -= 1
    if hy_w1 is not None:
        if hy_w1 <= -0.15:
            score += 1
        elif hy_w1 >= 0.15:
            score -= 1

    if score >= 3:
        return "Tight Credit / Risk-On", score
    if score <= -3:
        return "Stress Widening / Risk-Off", score
    return "Balanced Credit", score


def fetch_credit_monitor() -> dict[str, Any]:
    cached = _load_cache()
    if cached:
        return cached

    session = build_session()
    series: dict[str, dict[str, Any]] = {}
    for key, meta in CREDIT_SERIES.items():
        try:
            rows = _get_fred_series(session, meta["id"])
        except Exception:
            continue
        record = _record_from_series(meta, rows)
        if record:
            series[key] = record

    regime, score = _credit_regime(series)
    hy_oas = (series.get("hy_oas") or {}).get("latest")
    ig_oas = (series.get("ig_oas") or {}).get("latest")
    spread_gap = round(hy_oas - ig_oas, 3) if hy_oas is not None and ig_oas is not None else None

    result = {
        "series": series,
        "regime": regime,
        "score": score,
        "hy_minus_ig": spread_gap,
        "source": "FRED / ICE BofA and St. Louis Fed spread series",
        "is_real": True,
    }
    _save_cache(result)
    return result
