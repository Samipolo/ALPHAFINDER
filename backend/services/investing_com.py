"""
Investing.com Service — Windows-compatible.
Scrapes economic calendar, technical summaries, and quotes.
"""
from __future__ import annotations
import json
import os
import time
import re
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from bs4 import BeautifulSoup

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR
from services.net_utils import build_session, disable_dead_proxy_env

CACHE_ECON = os.path.join(CACHE_DIR, "inv_cal")
CACHE_TECH = os.path.join(CACHE_DIR, "inv_tech.json")
CACHE_QUOT = os.path.join(CACHE_DIR, "inv_quotes.json")

TTL_CAL  = 60
TTL_TECH = 300
TTL_QUOT = 60

BASE = "https://www.investing.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Referer":         "https://www.investing.com/",
}

JSON_HEADERS = dict(HEADERS)
JSON_HEADERS.update({
    "Accept": "application/json, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
})

CURRENCY_COUNTRY = {
    "USD": "5",  "EUR": "72", "GBP": "4",  "JPY": "35",
    "AUD": "25", "NZD": "43", "CAD": "6",  "CHF": "12",
}

EVENT_TO_FIELD = {
    "adp nonfarm":            "ADP",
    "adp employment":         "ADP",
    "gdp":                    "GDP",
    "gross domestic product": "GDP",
    "core retail sales":      "Retail Sales",
    "manufacturing pmi":      "mPMI",
    "s&p global manufacturing pmi": "mPMI",
    "caixin manufacturing pmi": "mPMI",
    "services pmi":           "sPMI",
    "s&p global services pmi": "sPMI",
    "composite pmi":          "sPMI",
    "retail sales":           "Retail Sales",
    "consumer confidence":    "Consumer Conf",
    "consumer sentiment":     "Consumer Conf",
    "consumer climate":       "Consumer Conf",
    "cpi":                    "CPI",
    "consumer price index":   "CPI",
    "inflation rate":         "CPI",
    "ppi":                    "PPI",
    "producer price index":   "PPI",
    "pce":                    "PCE",
    "personal consumption":   "PCE",
    "interest rate decision": "Interest Rates",
    "fed funds rate":         "Interest Rates",
    "bank rate":              "Interest Rates",
    "refinancing rate":       "Interest Rates",
    "monetary policy":        "Interest Rates",
    "nonfarm payrolls":       "NFP",
    "non-farm payrolls":      "NFP",
    "employment change":      "NFP",
    "unemployment rate":      "Unemployment Rate",
    "initial jobless claims": "Unemployment Claims",
    "jobless claims":         "Unemployment Claims",
}

VERDICT_SCORE = {
    "strong buy":  2, "strong_buy":  2,
    "buy":         1,
    "neutral":     0,
    "sell":       -1,
    "strong sell": -2, "strong_sell": -2,
}

def _load(path: str, ttl: int) -> Optional[object]:
    try:
        if os.path.exists(path):
            if time.time() - os.path.getmtime(path) < ttl:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
    except Exception:
        pass
    return None

def _save(path: str, data: object) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
    except Exception:
        pass


def _calendar_cache_path(days_back: int) -> str:
    days = max(1, int(days_back))
    return f"{CACHE_ECON}_{days}.json"


def _parse_event_datetime(value: str) -> Optional[datetime]:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _calendar_cache_is_current(events: list, max_age_days: int = 14) -> bool:
    latest = None
    for event in events or []:
        dt = _parse_event_datetime(str(event.get("datetime") or ""))
        if dt and (latest is None or dt > latest):
            latest = dt
    if latest is None:
        return False
    return latest.date() >= (datetime.utcnow().date() - timedelta(days=max_age_days))

def _session() -> requests.Session:
    return build_session(headers=HEADERS)

def _map_field(name: str) -> Optional[str]:
    n = name.lower().strip()
    for key, field in EVENT_TO_FIELD.items():
        if key in n:
            return field
    return None

def _clean_num(td) -> Optional[float]:
    if td is None:
        return None
    txt = td.get_text(strip=True)
    txt = txt.replace("%", "").replace("K", "000").replace("M", "000000").replace(",", "").strip()
    if not txt or txt in ("—", "-", ""):
        return None
    try:
        return float(txt)
    except (ValueError, TypeError):
        return None


def _event_score(event: dict) -> Optional[int]:
    field = event.get("field")
    act = event.get("actual")
    fore = event.get("forecast")
    prev = event.get("previous")
    if not field or act is None:
        return None

    score = 0
    if act is not None and fore is not None and fore != 0:
        rel = (act - fore) / abs(fore)
        if rel >= 0.04:
            score = 2
        elif rel >= 0.01:
            score = 1
        elif rel <= -0.04:
            score = -2
        elif rel <= -0.01:
            score = -1
    elif act is not None and prev is not None and prev != 0:
        rel = (act - prev) / abs(prev)
        if rel >= 0.02:
            score = 1
        elif rel <= -0.02:
            score = -1

    if field in ("Unemployment Rate", "Unemployment Claims"):
        score = -score

    try:
        impact = int(event.get("impact") or 2)
    except (TypeError, ValueError):
        impact = 2

    if impact >= 3 and score == 1:
        score = 2
    elif impact >= 3 and score == -1:
        score = -2
    return int(score)

def fetch_economic_calendar(days_back: int = 7) -> list:
    cache_path = _calendar_cache_path(days_back)
    cached = _load(cache_path, TTL_CAL)
    if cached is not None and _calendar_cache_is_current(cached):
        print("[InvCal] Returning cached calendar")
        return cached
    if cached is not None:
        print("[InvCal] Ignoring stale cached calendar")

    print("[InvCal] Fetching Investing.com calendar...")
    try:
        today = datetime.utcnow()
        date_from = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
        date_to   = today.strftime("%Y-%m-%d")

        s = _session()
        url = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
        payload = {
            "country[]":    list(CURRENCY_COUNTRY.values()),
            "importance[]": ["2", "3"],
            "dateFrom":     date_from,
            "dateTo":       date_to,
            "timeZone":     "0",
            "timeFilter":   "timeRemain",
            "currentTab":   "custom",
            "limit_from":   "0",
        }

        events = []
        seen = set()
        for limit_from in range(0, 1000, 200):
            payload["limit_from"] = str(limit_from)
            resp = s.post(url, data=payload, headers=JSON_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            html_data = data.get("data", "")
            if not html_data:
                if limit_from == 0:
                    raise ValueError("Empty response")
                break

            soup = BeautifulSoup(html_data, "lxml")
            rows = soup.find_all("tr", class_=re.compile(r"js-event-item"))
            if not rows:
                break

            page_added = 0
            for row in rows:
                try:
                    cur_td = row.find("td", class_="flagCur")
                    cur = (cur_td.get_text(strip=True).strip()[-3:] if cur_td else "")
                    if cur not in CURRENCY_COUNTRY:
                        continue
                    name_el = row.find("td", class_="event")
                    name = name_el.get_text(strip=True) if name_el else ""
                    key = (
                        cur,
                        name,
                        row.get("data-event-datetime", ""),
                        row.get("id", ""),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    act = _clean_num(row.find("td", id=re.compile(r"^eventActual_")))
                    fore = _clean_num(row.find("td", id=re.compile(r"^eventForecast_")))
                    prev = _clean_num(row.find("td", id=re.compile(r"^eventPrevious_")))
                    events.append(
                        {
                            "currency": cur,
                            "name": name,
                            "impact": "3",
                            "actual": act,
                            "forecast": fore,
                            "previous": prev,
                            "datetime": row.get("data-event-datetime", ""),
                            "field": _map_field(name),
                        }
                    )
                    page_added += 1
                except Exception:
                    continue

            if len(rows) < 200 or page_added == 0:
                break

        print(f"[InvCal] Got {len(events)} events")
        if events:
            _save(cache_path, events)
        return events

    except Exception as e:
        print(f"[InvCal] Error: {e}")
        stale = _load(cache_path, 999999)
        return stale if stale is not None and _calendar_cache_is_current(stale) else []

def get_calendar_scores(events: list) -> dict:
    scores = {}  # type: Dict[str, Dict[str, List[tuple[int, float]]]]
    latest_dt = None
    for ev in events:
        dt = _parse_event_datetime(str(ev.get("datetime") or ""))
        if dt and (latest_dt is None or dt > latest_dt):
            latest_dt = dt

    for ev in events:
        cur   = ev.get("currency", "")
        field = ev.get("field")
        act   = ev.get("actual")
        fore  = ev.get("forecast")
        prev  = ev.get("previous")
        if not cur or not field:
            continue
        if act is None:
            continue

        if cur not in scores:
            scores[cur] = {}
        if field not in scores[cur]:
            scores[cur][field] = []

        score = _event_score(ev)
        if score is None:
            continue
        impact = int(ev.get("impact") or 2)
        dt = _parse_event_datetime(str(ev.get("datetime") or ""))
        days_old = max(0, (latest_dt - dt).days) if latest_dt and dt else 0
        recency = max(0.4, 1.0 - (days_old * 0.12))
        weight = (1.0 if impact <= 1 else 1.35 if impact == 2 else 1.85) * recency
        scores[cur][field].append((score, weight))

    result = {}
    for cur, fields in scores.items():
        result[cur] = {}
        for f, vals in fields.items():
            if vals:
                weighted_total = sum(score * weight for score, weight in vals)
                total_weight = sum(weight for _, weight in vals)
                avg = weighted_total / max(total_weight, 1e-6)
                result[cur][f] = int(round(avg))
    return result


def get_latest_calendar_scores(events: list) -> dict:
    ranked = []
    for ev in events:
        field = ev.get("field")
        cur = ev.get("currency")
        score = _event_score(ev)
        if not cur or not field or score is None:
            continue
        ranked.append(ev)

    ranked.sort(key=lambda ev: ev.get("datetime") or "", reverse=True)
    result: Dict[str, Dict[str, int]] = {}
    for ev in ranked:
        cur = ev["currency"]
        field = ev["field"]
        result.setdefault(cur, {})
        if field in result[cur]:
            continue
        score = _event_score(ev)
        if score is None:
            continue
        result[cur][field] = score
    return result

def fetch_technical_summaries() -> dict:
    cached = _load(CACHE_TECH, TTL_TECH)
    if cached is not None:
        return cached

    print("[InvTech] Fetching technical summaries...")
    s = _session()
    results = {}

    PATHS = {
        "EURUSD": "/currencies/eur-usd-technical",
        "GBPUSD": "/currencies/gbp-usd-technical",
        "USDJPY": "/currencies/usd-jpy-technical",
        "AUDUSD": "/currencies/aud-usd-technical",
        "USDCAD": "/currencies/usd-cad-technical",
        "XAUUSD": "/commodities/gold-technical",
        "USOIL":  "/commodities/crude-oil-technical",
        "BTCUSD": "/crypto/bitcoin/btc-usd-technical",
        "SPX500": "/indices/us-spx-500-technical",
        "NAS100": "/indices/nq100-futures-technical",
        "DXY":    "/indices/us-dollar-index-technical",
    }

    for sym, path in PATHS.items():
        try:
            r = s.get(BASE + path, timeout=12)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")

            verdict_el = (
                soup.find("span", class_=re.compile(r"(summary|verdict|signal)", re.I)) or
                soup.find("div",  class_=re.compile(r"(summary|verdict|signal)", re.I))
            )
            verdict_txt = verdict_el.get_text(strip=True).lower() if verdict_el else "neutral"
            overall     = VERDICT_SCORE.get(verdict_txt, 0)

            # RSI
            rsi_val = None
            rsi_el  = soup.find(string=re.compile(r"RSI\s*\(14\)", re.I))
            if rsi_el and rsi_el.find_parent():
                nums = re.findall(r"\d+\.?\d*", rsi_el.find_parent().get_text())
                for n in nums:
                    v = float(n)
                    if 10 <= v <= 90:
                        rsi_val = v
                        break

            results[sym] = {
                "summary":  verdict_txt.title(),
                "overall":  overall,
                "ma_score": overall,
                "osc_score": overall,
                "rsi":      rsi_val,
            }
            time.sleep(0.4)

        except Exception as e:
            print(f"[InvTech] {sym}: {e}")
            continue

    print(f"[InvTech] Got {len(results)} summaries")
    if results:
        _save(CACHE_TECH, results)
    return results

def fetch_news() -> list:
    return []  # Removed news per user request

def fetch_quotes() -> dict:
    cached = _load(CACHE_QUOT, TTL_QUOT)
    if cached is not None:
        return cached

    disable_dead_proxy_env()
    s = _session()
    results = {}
    QUOTE_PATHS = {
        "EURUSD": "/currencies/eur-usd",
        "XAUUSD": "/commodities/gold",
        "BTCUSD": "/crypto/bitcoin",
        "SPX500": "/indices/us-spx-500",
        "USOIL":  "/commodities/crude-oil",
        "DXY":    "/indices/us-dollar-index",
    }

    for sym, path in QUOTE_PATHS.items():
        try:
            r = s.get(BASE + path, timeout=10)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            price_el = (
                soup.find("span", {"data-test": "instrument-price-last"}) or
                soup.find("div",  class_=re.compile(r"(last-price|price-last)", re.I))
            )
            chg_el = soup.find("span", {"data-test": "instrument-price-change-percent"})
            price = price_el.get_text(strip=True) if price_el else None
            chg   = chg_el.get_text(strip=True)   if chg_el   else None
            if price:
                results[sym] = {"price": price, "chg": chg}
            time.sleep(0.3)
        except Exception:
            continue

    if len(results) < len(QUOTE_PATHS):
        try:
            import yfinance as yf
            try:
                yf.set_tz_cache_location(CACHE_DIR)
            except Exception:
                pass
            try:
                import yfinance.cache as yf_cache

                yf_cache.set_cache_location(CACHE_DIR)
            except Exception:
                pass

            ticker_map = {
                "EURUSD": "EURUSD=X",
                "XAUUSD": "GC=F",
                "BTCUSD": "BTC-USD",
                "SPX500": "^GSPC",
                "USOIL": "CL=F",
                "DXY": "DX-Y.NYB",
            }
            needed = {sym: ticker for sym, ticker in ticker_map.items() if sym not in results}
            if needed:
                df = yf.download(
                    " ".join(needed.values()),
                    period="5d",
                    interval="1d",
                    group_by="ticker",
                    progress=False,
                    threads=False,
                    auto_adjust=True,
                )
                for sym, ticker in needed.items():
                    try:
                        close = df["Close"] if len(needed) == 1 else df[ticker]["Close"]
                        if hasattr(close, "iloc") and getattr(close, "ndim", 1) > 1:
                            close = close.iloc[:, 0]
                        close = close.dropna()
                        if len(close) < 1:
                            continue
                        price = float(close.iloc[-1])
                        prev = float(close.iloc[-2]) if len(close) >= 2 else price
                        change_pct = ((price - prev) / prev * 100) if prev else 0.0
                        results[sym] = {
                            "price": f"{price:,.5f}".rstrip("0").rstrip("."),
                            "chg": f"{change_pct:+.2f}%",
                        }
                    except Exception:
                        continue
        except Exception:
            pass

    if results:
        _save(CACHE_QUOT, results)
    return results
