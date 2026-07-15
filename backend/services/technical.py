"""
Technical Service — Real data via yfinance.
Windows-compatible: uses List/Dict from typing, no walrus operator.
Calculates SMA20/50/200 and 10-year seasonality from live OHLCV data.
"""
from __future__ import annotations
import json
import os
import time
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR, ALL_TICKERS
from services.net_utils import disable_dead_proxy_env

disable_dead_proxy_env()
import yfinance as yf

CACHE_FILE = os.path.join(CACHE_DIR, "technical.json")
CACHE_TTL  = 300  # 5 minutes


def _configure_yfinance_cache() -> None:
    try:
        yf.set_tz_cache_location(CACHE_DIR)
    except Exception:
        pass
    try:
        import yfinance.cache as yf_cache

        yf_cache.set_cache_location(CACHE_DIR)
    except Exception:
        pass

def _read_cache_payload(max_age_seconds: Optional[int] = None) -> Optional[dict]:
    try:
        if os.path.exists(CACHE_FILE):
            if max_age_seconds is not None and time.time() - os.path.getmtime(CACHE_FILE) >= max_age_seconds:
                return None
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return None
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            return data if isinstance(data, dict) else None
    except Exception:
        pass
    return None


def _load_cache() -> Optional[dict]:
    data = _read_cache_payload(CACHE_TTL)
    if not isinstance(data, dict):
        return None
    if len(data) < max(1, int(len(ALL_TICKERS) * 0.9)):
        return None
    return data


def _load_any_cache() -> Optional[dict]:
    data = _read_cache_payload(None)
    return data if isinstance(data, dict) and data else None

def _save_cache(data: dict) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"as_of": datetime.utcnow().isoformat() + "Z", "data": data}, f, default=str)
    except Exception as e:
        print(f"[Tech] Cache save error: {e}")

def _trend_score_a1(sma3: float, sma14: float, sma14_prev: float) -> int:
    """
    A1 Trading AlphaFinder trend score (−3 to +3).

    Step 1 — Crossover: SMA3 vs SMA14
      SMA3 > SMA14 → +2 (bullish)
      SMA3 < SMA14 → −2 (bearish)
      Equal → 0

    Step 2 — 14-day slope: compare current SMA14 to SMA14 from 3 bars ago
      Rising → +1
      Flat or falling → −1

    Step 3 — Momentum correction:
      Bullish crossover but falling slope → deduct 1
      Bearish crossover but rising slope → add 1
    """
    try:
        # Step 1: Crossover
        if sma3 > sma14:
            crossover = 2
        elif sma3 < sma14:
            crossover = -2
        else:
            crossover = 0

        # Step 2: 14-day slope direction
        slope_delta = sma14 - sma14_prev
        if slope_delta > 0:
            slope_score = 1
        else:
            slope_score = -1

        score = crossover + slope_score

        # Step 3: Momentum correction
        if crossover > 0 and slope_delta <= 0:
            score -= 1  # Bullish crossover but slope rolling over
        elif crossover < 0 and slope_delta > 0:
            score += 1  # Bearish crossover but slope recovering

        return max(-3, min(3, score))
    except Exception:
        return 0


def _seasonality_score(monthly_avgs: dict, month: int) -> int:
    """
    A1 Trading seasonality: binary ±2.
    10-year monthly average > 0 → +2, < 0 → −2, exactly 0 → 0.
    """
    avg = monthly_avgs.get(month, 0.0)
    if avg > 0:
        return 2
    elif avg < 0:
        return -2
    return 0

def fetch_technical() -> dict:
    disable_dead_proxy_env()
    _configure_yfinance_cache()
    cached = _load_cache()
    if cached is not None:
        print("[Tech] Returning cached data")
        return cached
    fallback_cache = _load_any_cache() or {}

    print("[Tech] Fetching price data via yfinance...")
    results = {}
    current_month = datetime.now().month

    # Build ticker -> symbols mapping
    ticker_to_syms = {}  # type: Dict[str, List[str]]
    for sym, tkr in ALL_TICKERS.items():
        if tkr not in ticker_to_syms:
            ticker_to_syms[tkr] = []
        ticker_to_syms[tkr].append(sym)

    unique_tickers = list(ticker_to_syms.keys())

    # Download in batches of 10 to avoid timeouts
    batch_size = 10
    for i in range(0, len(unique_tickers), batch_size):
        batch = unique_tickers[i:i + batch_size]
        batch_str = " ".join(batch)

        try:
            df = yf.download(
                batch_str,
                period="10y",
                interval="1d",
                group_by="ticker",
                progress=False,
                threads=False,  # threads=False is safer on Windows
                auto_adjust=True,
            )
        except Exception as e:
            print(f"[Tech] Batch download error: {e}")
            continue

        for tkr in batch:
            syms = ticker_to_syms[tkr]
            try:
                # Extract close prices
                if len(batch) == 1:
                    close = df["Close"]
                else:
                    if tkr not in df.columns.get_level_values(0):
                        continue
                    close = df[tkr]["Close"]

                # Flatten if DataFrame
                if isinstance(close, pd.DataFrame):
                    close = close.iloc[:, 0]

                close = close.dropna()
                if len(close) < 21:
                    continue

                price  = float(close.iloc[-1])
                n = len(close)

                # A1 Trading SMAs: 3-day (short-term) and 14-day (direction)
                sma3   = float(close.rolling(3).mean().iloc[-1]) if n >= 3 else price
                sma14  = float(close.rolling(14).mean().iloc[-1]) if n >= 14 else sma3
                # SMA14 from 3 bars ago for slope calculation
                sma14_series = close.rolling(14).mean() if n >= 14 else None
                if sma14_series is not None and len(sma14_series.dropna()) >= 4:
                    sma14_prev = float(sma14_series.dropna().iloc[-4])
                else:
                    sma14_prev = sma14

                # Display SMAs (kept for charts, not used in scoring)
                sma20  = float(close.rolling(20).mean().iloc[-1]) if n >= 20 else sma14
                sma50  = float(close.rolling(50).mean().iloc[-1]) if n >= 50 else sma20
                sma200 = float(close.rolling(200).mean().iloc[-1]) if n >= 200 else sma50

                # A1 trend score: crossover + slope + momentum correction
                trend = _trend_score_a1(sma3, sma14, sma14_prev)

                # 10-year monthly seasonality
                monthly_avgs = {}
                if len(close) > 240:
                    try:
                        # Use 'ME' for pandas >= 2.2, 'M' for older
                        try:
                            monthly = close.resample("ME").last().pct_change() * 100
                        except Exception:
                            monthly = close.resample("M").last().pct_change() * 100

                        month_groups = {}  # type: Dict[int, List[float]]
                        for idx, val in monthly.items():
                            if not np.isnan(float(val)):
                                m = idx.month
                                if m not in month_groups:
                                    month_groups[m] = []
                                month_groups[m].append(float(val))

                        monthly_avgs = {}
                        for m, vals in month_groups.items():
                            if vals:
                                monthly_avgs[m] = round(float(np.mean(vals)), 3)
                    except Exception as e2:
                        print(f"[Tech] Seasonality error for {tkr}: {e2}")

                # A1 Trading: binary ±2 seasonality
                seasonality = _seasonality_score(monthly_avgs, current_month)
                chg1d = float((close.iloc[-1] / close.iloc[-2] - 1) * 100) if n >= 2 else 0.0
                roc20 = float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if n >= 21 else 0.0

                rsi14 = 50.0
                if n >= 15:
                    delta = close.diff(1)
                    gain = delta.clip(lower=0)
                    loss = -delta.clip(upper=0)
                    avg_gain = gain.ewm(com=13, adjust=False).mean()
                    avg_loss = loss.ewm(com=13, adjust=False).mean()
                    rs = avg_gain / avg_loss
                    rsi = 100 - (100 / (1 + rs))
                    rsi14 = float(rsi.iloc[-1])
                    if np.isnan(rsi14) or np.isinf(rsi14):
                        rsi14 = 50.0

                record = {
                    "price":       round(price, 5),
                    "sma3":        round(sma3, 5),
                    "sma14":       round(sma14, 5),
                    "sma14_prev":  round(sma14_prev, 5),
                    "sma20":       round(sma20, 5),
                    "sma50":       round(sma50, 5),
                    "sma200":      round(sma200, 5),
                    "trend":       trend,
                    "seasonality": seasonality,
                    "chg1d":       round(chg1d, 3),
                    "roc20":       round(roc20, 3),
                    "rsi14":       round(rsi14, 2),
                    "monthly_avgs": monthly_avgs,
                }

                for sym in syms:
                    results[sym] = record

            except Exception as e:
                print(f"[Tech] Error processing {tkr}: {e}")
                continue

    print(f"[Tech] Computed data for {len(results)} symbols")
    if fallback_cache and len(fallback_cache) > len(results):
        merged = dict(fallback_cache)
        merged.update(results)
        if len(merged) > len(results):
            print(f"[Tech] Filled {len(merged) - len(results)} missing symbols from cached live data")
            results = merged
    if results:
        _save_cache(results)
    return results
