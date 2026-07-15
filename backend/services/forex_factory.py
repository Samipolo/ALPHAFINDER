"""
Forex Factory calendar service.
Fetches recent macro events and maps them onto the dashboard economic fields.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional

from bs4 import BeautifulSoup

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR
from services.investing_com import EVENT_TO_FIELD
from services.net_utils import build_session


CACHE_FILE = os.path.join(CACHE_DIR, "ff_cal.json")
CACHE_TTL = 60

CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}
BASE_URL = "https://www.forexfactory.com/calendar"


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


def _clean_num(text: str):
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


def _impact_score(cell) -> str:
    html = cell.decode_contents() if cell else ""
    if "impact-red" in html:
        return "3"
    if "impact-ora" in html or "impact-yel" in html:
        return "2"
    return "1"


def fetch_forexfactory_calendar(days_back: int = 7) -> list:
    cached = _load_cache()
    if cached is not None:
        print("[FFCal] Returning cached calendar")
        return cached

    print("[FFCal] Fetching Forex Factory calendar...")
    today = datetime.utcnow().date()
    wanted = set()
    for offset in range(days_back + 1):
        day = today - timedelta(days=offset)
        wanted.add(day.strftime("%a%b%d").lower())
        wanted.add(f"{day.strftime('%a')}{day.strftime('%b')}{day.day}".lower())

    try:
        session = _session()
        response = session.get(BASE_URL, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        rows = soup.select("tr.calendar__row")

        events = []
        current_day = ""
        for row in rows:
            classes = row.get("class", [])
            if "calendar__row--day-breaker" in classes:
                text = row.get_text(" ", strip=True)
                current_day = re.sub(r"[^A-Za-z0-9]", "", text).lower()
                continue

            date_cell = row.select_one("td.calendar__date")
            if date_cell:
                current_day = re.sub(r"[^A-Za-z0-9]", "", date_cell.get_text(" ", strip=True)).lower()

            if current_day and current_day not in wanted:
                continue

            currency = row.select_one("td.calendar__currency")
            event = row.select_one("td.calendar__event")
            impact = row.select_one("td.calendar__impact")
            actual = row.select_one("td.calendar__actual")
            forecast = row.select_one("td.calendar__forecast")
            previous = row.select_one("td.calendar__previous")
            time_cell = row.select_one("td.calendar__time")

            cur = currency.get_text(strip=True) if currency else ""
            name = event.get_text(" ", strip=True) if event else ""
            if cur not in CURRENCIES or not name:
                continue

            field = _map_field(name)
            if not field:
                continue

            events.append(
                {
                    "currency": cur,
                    "name": name,
                    "impact": _impact_score(impact),
                    "actual": _clean_num(actual.get_text(" ", strip=True) if actual else ""),
                    "forecast": _clean_num(forecast.get_text(" ", strip=True) if forecast else ""),
                    "previous": _clean_num(previous.get_text(" ", strip=True) if previous else ""),
                    "datetime": time_cell.get_text(" ", strip=True) if time_cell else "",
                    "field": field,
                    "source": "Forex Factory",
                }
            )

        if not events:
            print("[FFCal] Filtered day match returned no events, retrying without day gate")
            for row in rows:
                currency = row.select_one("td.calendar__currency")
                event = row.select_one("td.calendar__event")
                impact = row.select_one("td.calendar__impact")
                actual = row.select_one("td.calendar__actual")
                forecast = row.select_one("td.calendar__forecast")
                previous = row.select_one("td.calendar__previous")
                time_cell = row.select_one("td.calendar__time")

                cur = currency.get_text(strip=True) if currency else ""
                name = event.get_text(" ", strip=True) if event else ""
                if cur not in CURRENCIES or not name:
                    continue

                field = _map_field(name)
                if not field:
                    continue

                events.append(
                    {
                        "currency": cur,
                        "name": name,
                        "impact": _impact_score(impact),
                        "actual": _clean_num(actual.get_text(" ", strip=True) if actual else ""),
                        "forecast": _clean_num(forecast.get_text(" ", strip=True) if forecast else ""),
                        "previous": _clean_num(previous.get_text(" ", strip=True) if previous else ""),
                        "datetime": time_cell.get_text(" ", strip=True) if time_cell else "",
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
