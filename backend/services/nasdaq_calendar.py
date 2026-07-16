"""Nasdaq economic calendar service.

Nasdaq's public economic-events API (api.nasdaq.com/api/calendar/economicevents)
is the same host that reliably serves option chains from datacenter IPs, so it
works from Render where the Investing.com HTML scrape and the Forex Factory feed
intermittently come back empty. It carries actual / consensus (forecast) /
previous values for a rich set of releases, which is exactly what the surprise
meter and the economic scoring need.

Output rows match the shape produced by services.investing_com and
services.forex_factory (currency / name / impact / actual / forecast / previous
/ datetime / field / source) so all existing calendar consumers -- the merge,
the scorers, the alert pulse -- accept them unchanged.
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR
from services.investing_com import EVENT_TO_FIELD
from services.net_utils import build_session

CACHE_FILE = os.path.join(CACHE_DIR, "nasdaq_cal.json")
CACHE_TTL = 300

CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}

# Nasdaq reports the full country name; map the ones we track onto currencies.
# Euro-area member states all roll up to EUR.
COUNTRY_TO_CURRENCY = {
    "United States": "USD",
    "Euro Zone": "EUR", "Eurozone": "EUR", "European Union": "EUR",
    "Germany": "EUR", "France": "EUR", "Italy": "EUR", "Spain": "EUR",
    "Netherlands": "EUR", "Ireland": "EUR", "Portugal": "EUR", "Greece": "EUR",
    "Austria": "EUR", "Belgium": "EUR", "Finland": "EUR",
    "United Kingdom": "GBP",
    "Japan": "JPY",
    "Australia": "AUD",
    "New Zealand": "NZD",
    "Canada": "CAD",
    "Switzerland": "CHF",
}

# High / medium impact inferred from the event name, since this feed has no
# explicit importance flag. Anything not matched is treated as low ("1").
_HIGH_IMPACT_KEYS = (
    "nonfarm", "non-farm", "nfp", "cpi", "consumer price", "inflation rate",
    "gdp", "gross domestic", "interest rate decision", "rate decision", "fomc",
    "unemployment rate", "pce", "core pce", "retail sales", "ppi",
    "producer price", "ism", "employment change", "cash rate", "bank rate",
    "official cash rate", "federal funds",
)
_MED_IMPACT_KEYS = (
    "pmi", "manufacturing", "services", "consumer confidence", "consumer sentiment",
    "trade balance", "durable goods", "jobless claims", "initial claims",
    "building permits", "housing starts", "industrial production", "adp",
    "business confidence",
)


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
    if not value or value in ("-", "N/A", "Tentative"):
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


def _impact(name: str) -> str:
    lowered = (name or "").lower()
    if any(k in lowered for k in _HIGH_IMPACT_KEYS):
        return "3"
    if any(k in lowered for k in _MED_IMPACT_KEYS):
        return "2"
    return "1"


def _fetch_one_day(session, day: date) -> list:
    url = f"https://api.nasdaq.com/api/calendar/economicevents?date={day.strftime('%Y-%m-%d')}"
    rows = []
    for attempt in range(2):
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            rows = ((resp.json().get("data") or {}).get("rows")) or []
            break
        except Exception:
            if attempt == 0:
                time.sleep(0.6)  # brief backoff, then one retry
            else:
                raise
    events = []
    for row in rows:
        cur = COUNTRY_TO_CURRENCY.get((row.get("country") or "").strip())
        name = (row.get("eventName") or "").strip()
        if not cur or not name:
            continue
        gmt = (row.get("gmt") or "").strip()
        # gmt is "HH:MM" (24h); pair it with the queried date.
        try:
            hh, mm = gmt.split(":")
            dt_str = f"{day.strftime('%Y/%m/%d')} {int(hh):02d}:{int(mm):02d}:00"
        except (ValueError, AttributeError):
            dt_str = f"{day.strftime('%Y/%m/%d')} 00:00:00"
        events.append(
            {
                "currency": cur,
                "name": name,
                "impact": _impact(name),
                "actual": _clean_num(row.get("actual")),
                "forecast": _clean_num(row.get("consensus")),
                "previous": _clean_num(row.get("previous")),
                "datetime": dt_str,
                "field": _map_field(name),
                "source": "Nasdaq",
            }
        )
    return events


def fetch_nasdaq_calendar(days_back: int = 7, days_fwd: int = 2) -> list:
    cached = _load_cache()
    if cached is not None:
        print("[NasdaqCal] Returning cached calendar")
        return cached

    print("[NasdaqCal] Fetching Nasdaq economic calendar...")
    today = datetime.utcnow().date()
    days = [today + timedelta(days=off) for off in range(-days_back, days_fwd + 1)]

    # Fetched sequentially on purpose: api.nasdaq.com rate-limits bursts (the
    # same behaviour the option endpoints hit), and this runs inside the
    # already-parallel background refresh, so adding our own thread pool just
    # invited 429s during the startup stampede. One-at-a-time with a short
    # retry is a few seconds slower but reliably returns the full calendar.
    events: list = []
    days_ok = 0
    session = build_session()
    for d in days:
        try:
            day_events = _fetch_one_day(session, d)
            events.extend(day_events)
            days_ok += 1
        except Exception as exc:
            print(f"[NasdaqCal] {d} error: {exc}")
    print(f"[NasdaqCal] Got {len(events)} events across {days_ok}/{len(days)} days")
    if events:
        _save_cache(events)
        return events
    stale = _load_cache()
    return stale if stale is not None else events