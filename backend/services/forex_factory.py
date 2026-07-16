"""
Forex Factory calendar service.
Fetches recent macro events and maps them onto the dashboard economic fields.

Uses Forex Factory's own public JSON widget feed (nfs.faireconomy.media)
instead of scraping forexfactory.com/calendar directly. The HTML calendar
page sits behind Cloudflare bot protection that silently blocks/empties
requests from datacenter IP ranges (Render included) -- it returns 200 with
zero parseable rows rather than an error, so the scraper looked "successful"
while quietly delivering nothing. The JSON feed is the same data Forex
Factory itself serves to embeddable calendar widgets and isn't behind that
wall.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR
from services.investing_com import EVENT_TO_FIELD
from services.net_utils import build_session


CACHE_FILE = os.path.join(CACHE_DIR, "ff_cal.json")
CACHE_TTL = 60

CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def _load_cache() -> Optional[list]:
    try:
        if os.path.exists(CACHE_FILE):
            if time.time() - os.path.getmtime(CACHE_FILE) < CACHE_TTL:
                with open(CACHE_FILE, "r", encoding="utf-8") as handle:
                    return json.load(handle)
    except Exception:
        pass
    return None


def _save_cache(data: list) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(data, handle, default=str)
    except Exception:
        pass


def _session():
    return build_session()


def _clean_num(text):
    if text is None:
        return None
    value = (
        str(text)
        .replace(",", "")
        .replace("%", "")
        .replace("K", "000")
        .replace("M", "000000")
        .replace("B", "000000000")
        .replace("T", "000000000000")
        .strip()
    )
    if not value or value in ("-", "—", "Tentative"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _map_field(name: str):
    lowered = (name or "").lower().strip()
    for key, field in EVENT_TO_FIELD.items():
        if key in lowered:
            return field
    return None


def _impact_from_label(label) -> str:
    lowered = (label or "").strip().lower()
    if lowered == "high":
        return "3"
    if lowered == "medium":
        return "2"
    return "1"


def fetch_forexfactory_calendar(days_back: int = 7) -> list:
    cached = _load_cache()
    if cached is not None:
        print("[FFCal] Returning cached calendar")
        return cached

    print("[FFCal] Fetching Forex Factory calendar (public JSON feed)...")
    cutoff = datetime.utcnow().date() - timedelta(days=days_back)

    try:
        session = _session()
        response = session.get(FEED_URL, timeout=15)
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, list):
            raise ValueError("unexpected feed shape")

        events = []
        for item in raw:
            cur = (item.get("country") or "").strip().upper()
            name = (item.get("title") or "").strip()
            if cur not in CURRENCIES or not name:
                continue
            field = _map_field(name)
            if not field:
                continue

            date_str = item.get("date") or ""
            event_dt = None
            try:
                event_dt = datetime.fromisoformat(date_str)
            except ValueError:
                pass
            if event_dt and event_dt.date() < cutoff:
                continue

            events.append(
                {
                    "currency": cur,
                    "name": name,
                    "impact": _impact_from_label(item.get("impact")),
                    "actual": _clean_num(item.get("actual")),
                    "forecast": _clean_num(item.get("forecast")),
                    "previous": _clean_num(item.get("previous")),
                    "datetime": event_dt.strftime("%I:%M%p").lstrip("0").lower() if event_dt else "",
                    "field": field,
                    "source": "Forex Factory",
                }
            )

        print(f"[FFCal] Got {len(events)} events")
        if events:
            _save_cache(events)
        return events
    except Exception as exc:
        print(f"[FFCal] Error: {exc}")
        stale = _load_cache()
        return stale if stale is not None else []