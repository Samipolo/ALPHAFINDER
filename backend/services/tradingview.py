"""
TradingView trend service.
Builds 4H + 1D trend scores for the dashboard.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR


CACHE_FILE = os.path.join(CACHE_DIR, "tradingview_trend.json")
CACHE_TTL = 900

TV_SYMBOLS = {
    "EURUSD": ("forex", "FX_IDC", "EURUSD"),
    "GBPUSD": ("forex", "FX_IDC", "GBPUSD"),
    "USDJPY": ("forex", "FX_IDC", "USDJPY"),
    "AUDUSD": ("forex", "FX_IDC", "AUDUSD"),
    "NZDUSD": ("forex", "FX_IDC", "NZDUSD"),
    "USDCAD": ("forex", "FX_IDC", "USDCAD"),
    "USDCHF": ("forex", "FX_IDC", "USDCHF"),
    "EURJPY": ("forex", "FX_IDC", "EURJPY"),
    "GBPJPY": ("forex", "FX_IDC", "GBPJPY"),
    "AUDJPY": ("forex", "FX_IDC", "AUDJPY"),
    "NZDJPY": ("forex", "FX_IDC", "NZDJPY"),
    "EURAUD": ("forex", "FX_IDC", "EURAUD"),
    "GBPAUD": ("forex", "FX_IDC", "GBPAUD"),
    "EURGBP": ("forex", "FX_IDC", "EURGBP"),
    "AUDCAD": ("forex", "FX_IDC", "AUDCAD"),
    "NZDCAD": ("forex", "FX_IDC", "NZDCAD"),
    "CADJPY": ("forex", "FX_IDC", "CADJPY"),
    "GBPCAD": ("forex", "FX_IDC", "GBPCAD"),
    "AUDNZD": ("forex", "FX_IDC", "AUDNZD"),
    "EURNZD": ("forex", "FX_IDC", "EURNZD"),
    "GBPNZD": ("forex", "FX_IDC", "GBPNZD"),
    "CHFJPY": ("forex", "FX_IDC", "CHFJPY"),
    "SPX500": ("america", "SP", "SPX"),
    "NAS100": ("america", "NASDAQ", "NDX"),
    "US30": ("america", "DJ", "DJI"),
    "XAUUSD": ("cfd", "OANDA", "XAUUSD"),
    "BTCUSD": ("crypto", "BITSTAMP", "BTCUSD"),
    "DXY": ("cfd", "TVC", "DXY"),
}


def _load_cache(allow_stale: bool = False) -> Optional[dict]:
    try:
        if os.path.exists(CACHE_FILE):
            if allow_stale or time.time() - os.path.getmtime(CACHE_FILE) < CACHE_TTL:
                with open(CACHE_FILE, "r", encoding="utf-8") as handle:
                    return json.load(handle)
    except Exception:
        pass
    return None


def _save_cache(data: dict) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(data, handle, default=str)
    except Exception:
        pass


def _score_summary(summary: dict) -> int:
    if not summary:
        return 0
    buy = int(summary.get("BUY", 0) or 0)
    sell = int(summary.get("SELL", 0) or 0)
    neutral = int(summary.get("NEUTRAL", 0) or 0)
    total = buy + sell + neutral
    if total <= 0:
        return 0
    bias = (buy - sell) / float(total)
    if bias >= 0.45:
        return 2
    if bias >= 0.10:
        return 1
    if bias <= -0.45:
        return -2
    if bias <= -0.10:
        return -1
    return 0


def fetch_tradingview_trends() -> Dict[str, dict]:
    cached = _load_cache()
    if cached is not None:
        print("[TVTrend] Returning cached data")
        return cached
    stale_cache = _load_cache(allow_stale=True)

    print("[TVTrend] Fetching TradingView 4H/1D trend scores...")
    try:
        from tradingview_ta import Interval, TA_Handler
    except Exception as exc:
        print(f"[TVTrend] tradingview-ta unavailable: {exc}")
        return {}

    results = {}
    for sym, (screener, exchange, tv_symbol) in TV_SYMBOLS.items():
        try:
            handler_4h = TA_Handler(
                symbol=tv_symbol,
                screener=screener,
                exchange=exchange,
                interval=Interval.INTERVAL_4_HOURS,
            )
            handler_1d = TA_Handler(
                symbol=tv_symbol,
                screener=screener,
                exchange=exchange,
                interval=Interval.INTERVAL_1_DAY,
            )
            analysis_4h = handler_4h.get_analysis()
            analysis_1d = handler_1d.get_analysis()
            score_4h = _score_summary(analysis_4h.summary)
            score_1d = _score_summary(analysis_1d.summary)
            blended = round((score_4h * 0.45) + (score_1d * 0.55))
            if blended > 2:
                blended = 2
            if blended < -2:
                blended = -2

            results[sym] = {
                "score_4h": score_4h,
                "score_1d": score_1d,
                "trend": int(blended),
                "summary_4h": analysis_4h.summary.get("RECOMMENDATION", "NEUTRAL"),
                "summary_1d": analysis_1d.summary.get("RECOMMENDATION", "NEUTRAL"),
                "source": "TradingView",
            }
            time.sleep(0.35)
        except Exception as exc:
            print(f"[TVTrend] {sym}: {exc}")
            continue

    if results:
        _save_cache(results)
        return results
    if stale_cache is not None:
        print("[TVTrend] Falling back to stale cache")
        return stale_cache
    return {}
