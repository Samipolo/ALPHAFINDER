"""
Retail Sentiment Service — Windows-compatible.
Primary: Myfxbook Community Outlook
Fallback: Dukascopy, then sourced data (April 2026)
"""
from __future__ import annotations
import json
import os
import time
import re
import requests
from typing import List, Optional
from bs4 import BeautifulSoup

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR
from services.net_utils import build_session

CACHE_FILE = os.path.join(CACHE_DIR, "retail.json")
CACHE_TTL  = 1800  # 30 minutes

MYFXBOOK_SYMS = {
    "EURUSD","GBPUSD","USDJPY","AUDUSD","NZDUSD","USDCAD",
    "USDCHF","EURJPY","GBPJPY","EURAUD","GBPAUD","EURGBP",
    "AUDCAD","NZDCAD","CADJPY","GBPCAD","AUDNZD","EURNZD",
    "GBPNZD","CHFJPY","XAUUSD","XAGUSD",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def _load_cache() -> Optional[list]:
    try:
        if os.path.exists(CACHE_FILE):
            if time.time() - os.path.getmtime(CACHE_FILE) < CACHE_TTL:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
    except Exception:
        pass
    return None

def _save_cache(data: list) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
    except Exception as e:
        print(f"[Retail] Cache save error: {e}")


def _session():
    return build_session(headers=HEADERS)

def _try_myfxbook_api() -> list:
    url = "https://www.myfxbook.com/api/get-community-outlook.json"
    try:
        resp = _session().get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results = []
        symbols = data.get("symbols", [])
        for s in symbols:
            name = (s.get("name") or "").replace("/", "").upper()
            if name not in MYFXBOOK_SYMS:
                continue
            try:
                lp = float(s.get("longPercentage", 50))
                sp = float(s.get("shortPercentage", 50))
                results.append({
                    "symbol":    name,
                    "long_pct":  round(lp, 2),
                    "short_pct": round(sp, 2),
                    "source":    "Myfxbook API",
                })
            except (ValueError, TypeError):
                continue
        return results
    except Exception as e:
        print(f"[Retail] Myfxbook API failed: {e}")
        return []


def _try_myfxbook_html() -> list:
    url = "https://www.myfxbook.com/community/outlook"
    try:
        session = _session()
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        results = []
        for row in soup.select("tr.outlook-symbol-row"):
            sym = (row.get("symbolname") or "").replace("/", "").upper()
            if sym not in MYFXBOOK_SYMS:
                continue
            popover = row.find("div", id=re.compile(r"^outlookSymbolPopover"))
            if not popover:
                continue
            action_map = {}
            for inner in popover.select("tbody tr"):
                cells = [cell.get_text(" ", strip=True) for cell in inner.find_all("td")]
                if len(cells) < 2:
                    continue
                if len(cells) >= 5:
                    action = cells[1].strip().lower()
                    pct_text = cells[2].replace("%", "").strip()
                else:
                    action = cells[0].strip().lower()
                    pct_text = cells[1].replace("%", "").strip()
                try:
                    action_map[action] = float(pct_text)
                except (TypeError, ValueError):
                    continue
            long_pct = action_map.get("long")
            short_pct = action_map.get("short")
            if long_pct is None or short_pct is None:
                continue
            results.append(
                {
                    "symbol": sym,
                    "long_pct": round(long_pct, 2),
                    "short_pct": round(short_pct, 2),
                    "source": "Myfxbook HTML",
                }
            )
        return results
    except Exception as e:
        print(f"[Retail] Myfxbook HTML failed: {e}")
        return []

def _try_dukascopy() -> list:
    url = "https://www.dukascopy.com/trading-tools/widgets/sentiment_index/data.json"
    try:
        resp = _session().get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data:
            sym = (item.get("instrument") or "").replace("/", "").upper()
            if sym not in MYFXBOOK_SYMS:
                continue
            try:
                lp = float(item.get("longs", 50))
                sp = float(item.get("shorts", 50))
                total = lp + sp or 100.0
                results.append({
                    "symbol":    sym,
                    "long_pct":  round(lp / total * 100, 2),
                    "short_pct": round(sp / total * 100, 2),
                    "source":    "Dukascopy",
                })
            except (ValueError, TypeError):
                continue
        return results
    except Exception as e:
        print(f"[Retail] Dukascopy failed: {e}")
        return []


def _try_fxssi() -> list:
    url = "https://fxssi.com/tools/current-ratio"
    try:
        resp = _session().get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        results = []
        seen = set()
        for row in soup.select(".sidebar .sentiment-ratios .line"):
            sym_el = row.select_one(".symbol")
            left_el = row.select_one(".ratio-bar-left")
            right_el = row.select_one(".ratio-bar-right")
            if not sym_el or not left_el or not right_el:
                continue
            sym = sym_el.get_text(" ", strip=True).replace("/", "").upper()
            if sym not in MYFXBOOK_SYMS or sym in seen:
                continue
            left_text = left_el.get_text(" ", strip=True).replace("%", "")
            right_text = right_el.get_text(" ", strip=True).replace("%", "")
            try:
                long_pct = float(left_text)
                short_pct = float(right_text)
            except (TypeError, ValueError):
                continue
            total = long_pct + short_pct
            if total <= 0:
                continue
            seen.add(sym)
            results.append({
                "symbol": sym,
                "long_pct": round(long_pct / total * 100, 2),
                "short_pct": round(short_pct / total * 100, 2),
                "source": "FXSSI Quick Sentiment",
            })
        return results
    except Exception as e:
        print(f"[Retail] FXSSI failed: {e}")
        return []

def _try_binance_crypto() -> list:
    """Real retail positioning: Binance futures global long/short account ratio."""
    results = []
    for pair, sym in (("BTCUSDT", "BTCUSD"), ("ETHUSDT", "ETHUSD")):
        try:
            url = ("https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
                   f"?symbol={pair}&period=1d&limit=1")
            resp = _session().get(url, timeout=12)
            resp.raise_for_status()
            rows = resp.json()
            if rows:
                lp = float(rows[0]["longAccount"]) * 100
                results.append({
                    "symbol": sym,
                    "long_pct": round(lp, 2),
                    "short_pct": round(100 - lp, 2),
                    "source": "Binance Futures L/S Accounts",
                })
        except Exception as e:
            print(f"[Retail] Binance {pair} failed: {e}")
    return results


def _try_fear_greed() -> list:
    """Crypto Fear & Greed index (alternative.me) as an extra sentiment row."""
    try:
        resp = _session().get("https://api.alternative.me/fng/?limit=1", timeout=12)
        resp.raise_for_status()
        row = resp.json()["data"][0]
        return [{
            "symbol": "CRYPTO_FEAR_GREED",
            "long_pct": float(row["value"]),
            "short_pct": round(100 - float(row["value"]), 2),
            "classification": row.get("value_classification"),
            "source": "alternative.me Fear & Greed",
        }]
    except Exception as e:
        print(f"[Retail] Fear&Greed failed: {e}")
        return []


def fetch_retail() -> list:
    cached = _load_cache()
    if cached is not None:
        if isinstance(cached, list):
            print("[Retail] Returning cached data")
            return cached

    print("[Retail] Fetching retail sentiment...")

    extras = _try_binance_crypto() + _try_fear_greed()

    results = _try_myfxbook_html()
    if len(results) >= 10:
        print(f"[Retail] Got {len(results)} from Myfxbook HTML")
        results += extras
        _save_cache(results)
        return results

    results = _try_myfxbook_api()
    if len(results) >= 10:
        print(f"[Retail] Got {len(results)} from Myfxbook API")
        results += extras
        _save_cache(results)
        return results

    results = _try_fxssi()
    if len(results) >= 8:
        print(f"[Retail] Got {len(results)} from FXSSI")
        results += extras
        _save_cache(results)
        return results

    results = _try_dukascopy()
    if len(results) >= 8:
        print(f"[Retail] Got {len(results)} from Dukascopy")
        results += extras
        _save_cache(results)
        return results

    stale = _load_cache()
    if stale is not None:
        print("[Retail] No fresh retail sentiment; using stale cached live data")
        return stale

    print("[Retail] No live retail sentiment data available")
    return []
