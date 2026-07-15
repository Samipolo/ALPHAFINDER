"""
COT Service - pulls 100% real data from the official CFTC Socrata API.
Tracks both the Tuesday report date and the Friday publication timestamp.

CFTC Release Schedule (from cftc.gov/MarketReports/CommitmentsofTraders/ReleaseSchedule):
  - Data is collected every Tuesday (the "report date").
  - Reports are published every Friday at 3:30 PM Eastern Time.
  - Federal holidays may delay release by one or two days (Mon→Tue shift).
  - The Socrata API (jun7-fc8e) is updated shortly after publication.

Refresh strategy:
  - On startup: always fetch fresh from CFTC (ignore cache).
  - Every 5 min: re-check the CFTC Socrata API for new data.
  - Cache TTL is 4 min to stay under the 5-min polling cycle.
  - Friday 3-6 PM ET: ultra-aggressive 2-min cache (publication window).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR, COT_KEYS
from services.net_utils import build_session

CACHE_FILE = os.path.join(CACHE_DIR, "cot.json")

# ── Cache TTLs ────────────────────────────────────────────────────────
# These are intentionally short to ensure data freshness within 5-min cycles.
BASE_CACHE_TTL = 4 * 60             # 4 min on quiet days (fits inside 5-min poll)
ACTIVE_DAY_CACHE_TTL = 4 * 60      # 4 min on Tue/Fri (always under 5-min poll)
PUBLISH_WINDOW_TTL = 2 * 60         # 2 min during Fri 3-6 PM ET window
ACTIVE_REFRESH_WEEKDAYS = {1, 4}    # Tuesday, Friday in Python weekday numbering
DAY_NAMES = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
EASTERN_TZ = ZoneInfo("America/New_York")

CFTC_DATA_URL = (
    "https://publicreporting.cftc.gov/resource/jun7-fc8e.json"
    "?$limit=800&$order=report_date_as_yyyy_mm_dd%20DESC"
)
CFTC_MARKER_URL = (
    "https://publicreporting.cftc.gov/resource/jun7-fc8e.json"
    "?$limit=1"
    "&$select=report_date_as_yyyy_mm_dd,:created_at,:updated_at"
    "&$order=report_date_as_yyyy_mm_dd%20DESC"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


def _normalize_report_date(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return text.split("T", 1)[0]


def _normalize_marker(marker: Optional[dict[str, Any]]) -> dict[str, str]:
    marker = marker or {}
    return {
        "report_date": _normalize_report_date(
            marker.get("report_date")
            or marker.get("report_date_as_yyyy_mm_dd")
        ),
        "source_published_at": str(
            marker.get("source_published_at") or marker.get(":created_at") or ""
        ).strip(),
        "source_updated_at": str(
            marker.get("source_updated_at") or marker.get(":updated_at") or ""
        ).strip(),
    }


def _read_cache_payload() -> Optional[dict[str, Any]]:
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, list):
            return {"data": payload, "marker": {}}
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                payload["marker"] = _normalize_marker(payload.get("marker"))
                return payload
    except Exception:
        pass
    return None


def _extract_cache_data(payload: Optional[dict[str, Any]]) -> list:
    if not payload:
        return []
    data = payload.get("data")
    return data if isinstance(data, list) else []


def _is_publish_window(eastern_now: datetime) -> bool:
    """Friday 3:00 PM - 6:00 PM ET is when CFTC typically publishes."""
    return eastern_now.weekday() == 4 and 15 <= eastern_now.hour < 18


def _current_cache_ttl(now: Optional[datetime] = None) -> int:
    current = now or _utc_now()
    eastern_now = current.astimezone(EASTERN_TZ)
    if _is_publish_window(eastern_now):
        return PUBLISH_WINDOW_TTL
    if eastern_now.weekday() in ACTIVE_REFRESH_WEEKDAYS:
        return ACTIVE_DAY_CACHE_TTL
    return BASE_CACHE_TTL


def _cache_is_fresh(now: Optional[datetime] = None) -> bool:
    try:
        if not os.path.exists(CACHE_FILE):
            return False
        age_seconds = time.time() - os.path.getmtime(CACHE_FILE)
        return age_seconds < _current_cache_ttl(now=now)
    except Exception:
        return False


def _save_cache(data: list, marker: Optional[dict[str, Any]] = None) -> None:
    payload = {
        "fetched_at": _utc_now_iso(),
        "marker": _normalize_marker(marker),
        "data": data,
    }
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, default=str)
    except Exception as exc:
        print(f"[COT] Cache save error: {exc}")


def _apply_marker_to_rows(rows: list[dict[str, Any]], marker: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    meta = _normalize_marker(marker)
    if not rows:
        return rows

    enriched: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        updated = dict(row)
        updated["report_date"] = _normalize_report_date(updated.get("report_date")) or meta["report_date"]
        if meta["source_published_at"]:
            updated["source_published_at"] = meta["source_published_at"]
        if meta["source_updated_at"]:
            updated["source_updated_at"] = meta["source_updated_at"]
        enriched.append(updated)
    return enriched


def _fetch_source_marker(session) -> Optional[dict[str, Any]]:
    response = session.get(CFTC_MARKER_URL, timeout=20)
    response.raise_for_status()
    rows = response.json()
    if isinstance(rows, list) and rows:
        return rows[0]
    return None


def invalidate_cache() -> None:
    """Force-invalidate the COT cache so the next fetch_cot() call refetches from CFTC."""
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
            print("[COT] Cache invalidated (file deleted)")
    except Exception as exc:
        print(f"[COT] Cache invalidation error: {exc}")


def fetch_cot() -> list:
    eastern_now = _utc_now().astimezone(EASTERN_TZ)
    day_name = DAY_NAMES.get(eastern_now.weekday(), "?")
    is_active_day = eastern_now.weekday() in ACTIVE_REFRESH_WEEKDAYS
    in_pub_window = _is_publish_window(eastern_now)

    cached_payload = _read_cache_payload()
    cached_data = _apply_marker_to_rows(
        _extract_cache_data(cached_payload),
        (cached_payload or {}).get("marker"),
    )

    # Only use cache if it's truly fresh (within TTL)
    if cached_data and _cache_is_fresh():
        ttl = _current_cache_ttl()
        print(f"[COT] Returning cached data ({day_name}, TTL={ttl}s, pub_window={in_pub_window})")
        return cached_data

    print(f"[COT] Cache stale or empty — fetching from CFTC ({day_name}, active={is_active_day}, pub_window={in_pub_window})...")
    session = build_session()
    marker: Optional[dict[str, Any]] = None

    try:
        marker = _fetch_source_marker(session)
        if marker:
            live_date = _normalize_marker(marker)["report_date"]
            cached_date = _normalize_report_date(cached_data[0].get("report_date")) if cached_data else ""
            print(f"[COT] CFTC latest report_date: {live_date}, cached report_date: {cached_date}")

            # If the report dates match and data hasn't changed, just refresh timestamps
            if cached_data and cached_date == live_date and not is_active_day:
                print(f"[COT] Same report date on {day_name} — refreshing cache timestamp")
                refreshed = _apply_marker_to_rows(cached_data, marker)
                _save_cache(refreshed, marker)
                return refreshed
    except Exception as exc:
        print(f"[COT] Marker check failed: {exc}")

    # Full fetch from CFTC
    print(f"[COT] Fetching full CFTC dataset ({day_name})...")
    try:
        response = session.get(CFTC_DATA_URL, timeout=30)
        response.raise_for_status()
        raw = response.json()
        result = _parse_cot(raw, marker=marker)
        if result:
            print(f"[COT] Fetched {len(result)} instruments — report_date: {result[0].get('report_date', '?')}")
            _save_cache(result, marker)
            return result
        if cached_data:
            print("[COT] Live parse returned no rows; using cached data")
            _save_cache(cached_data, (cached_payload or {}).get("marker"))
            return cached_data
        return []
    except Exception as exc:
        print(f"[COT] Fetch error: {exc}")
        if cached_data:
            return cached_data
        return []


def _parse_cot(raw: list, marker: Optional[dict[str, Any]] = None) -> list:
    seen = set()
    out = []
    meta = _normalize_marker(marker)

    for row in raw:
        if not isinstance(row, dict):
            continue
        name = (row.get("market_and_exchange_names") or "").upper()
        report_date = _normalize_report_date(row.get("report_date_as_yyyy_mm_dd")) or meta["report_date"]

        for sym, keys in COT_KEYS.items():
            if sym in seen:
                continue
            if not any(key.upper() in name for key in keys):
                continue

            try:
                long_contracts = int(row.get("noncomm_positions_long_all") or 0)
                short_contracts = int(row.get("noncomm_positions_short_all") or 0)
                open_interest = int(row.get("open_interest_all") or 0) or (long_contracts + short_contracts)
                delta_long = int(row.get("change_in_noncomm_long_all") or 0)
                delta_short = int(row.get("change_in_noncomm_short_all") or 0)
            except (TypeError, ValueError):
                continue

            total_contracts = (long_contracts + short_contracts) or 1
            long_pct = long_contracts / total_contracts * 100.0
            short_pct = 100.0 - long_pct

            prev_long = long_contracts - delta_long
            prev_short = short_contracts - delta_short
            prev_total = (prev_long + prev_short) or 1
            prev_long_pct = prev_long / prev_total * 100.0
            net_change = (long_pct - short_pct) - (prev_long_pct - (100.0 - prev_long_pct))

            seen.add(sym)
            row_out = {
                "symbol": sym,
                "long_contracts": long_contracts,
                "short_contracts": short_contracts,
                "delta_long": delta_long,
                "delta_short": delta_short,
                "long_pct": f"{long_pct:.1f}%",
                "short_pct": f"{short_pct:.1f}%",
                "long_pct_raw": round(long_pct, 2),
                "short_pct_raw": round(short_pct, 2),
                "net_pct_change_raw": round(net_change, 2),
                "net_pct_change": ("+" if net_change > 0 else "") + f"{net_change:.2f}%",
                "net_position": long_contracts - short_contracts,
                "open_interest": open_interest,
                "delta_oi": delta_long - delta_short,
                "report_date": report_date,
            }
            if meta["source_published_at"]:
                row_out["source_published_at"] = meta["source_published_at"]
            if meta["source_updated_at"]:
                row_out["source_updated_at"] = meta["source_updated_at"]
            out.append(row_out)
            break

    print(f"[COT] Parsed {len(out)} instruments")
    return out
