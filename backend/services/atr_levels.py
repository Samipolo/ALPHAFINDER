from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import ALL_TICKERS, CACHE_DIR, FOREX_PAIRS
from services.net_utils import disable_dead_proxy_env


CACHE_FILE = os.path.join(CACHE_DIR, "daily_atr.json")


def _configure_yfinance_cache() -> None:
    disable_dead_proxy_env()
    try:
        yf.set_tz_cache_location(CACHE_DIR)
    except Exception:
        pass
    try:
        import yfinance.cache as yf_cache

        yf_cache.set_cache_location(CACHE_DIR)
    except Exception:
        pass


def _cache_is_current(payload: dict[str, Any]) -> bool:
    as_of = str(payload.get("as_of") or "")
    if not as_of:
        return False
    data = payload.get("data") or []
    if len(data) < max(1, int(len(ALL_TICKERS) * 0.9)):
        return False
    try:
        cached_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    return (now - cached_dt).total_seconds() < 3600


def _load_cache() -> list[dict[str, Any]] | None:
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or not _cache_is_current(payload):
            return None
        data = payload.get("data")
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _load_any_cache() -> list[dict[str, Any]] | None:
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _save_cache(data: list[dict[str, Any]]) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "as_of": datetime.now(timezone.utc).isoformat(),
                    "data": data,
                },
                handle,
                default=str,
            )
    except Exception:
        pass


def _pip_factor(symbol: str) -> int | None:
    if symbol in FOREX_PAIRS:
        return 100 if symbol.endswith("JPY") else 10000
    return None


def _vol_regime(atr14: float, atr63_mean: float) -> str:
    if atr63_mean <= 0:
        return "Stable"
    ratio = atr14 / atr63_mean
    if ratio >= 1.15:
        return "Expanding"
    if ratio <= 0.85:
        return "Contracting"
    return "Stable"


def _format_record(symbol: str, frame: pd.DataFrame) -> dict[str, Any] | None:
    if frame is None or frame.empty:
        return None
    data = frame.copy()
    if isinstance(data, pd.Series):
        return None
    data = data.dropna(subset=["High", "Low", "Close"])
    if len(data) < 20:
        return None

    prev_close = data["Close"].shift(1)
    true_range = pd.concat(
        [
            (data["High"] - data["Low"]).abs(),
            (data["High"] - prev_close).abs(),
            (data["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr14 = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    atr63_avg = atr14.rolling(63).mean()

    price = float(data["Close"].iloc[-1])
    atr_value = float(atr14.iloc[-1])
    as_of = data.index[-1]
    atr_pct = (atr_value / price * 100.0) if price else 0.0
    pip_factor = _pip_factor(symbol)

    record: dict[str, Any] = {
        "symbol": symbol,
        "price": round(price, 5),
        "atr14": round(atr_value, 5),
        "atr_pct": round(atr_pct, 2),
        "expected_low": round(price - atr_value, 5),
        "expected_high": round(price + atr_value, 5),
        "regime": _vol_regime(atr_value, float(atr63_avg.iloc[-1]) if pd.notna(atr63_avg.iloc[-1]) else 0.0),
        "as_of": as_of.strftime("%Y-%m-%d"),
        "source": "yfinance daily OHLC",
        "is_real": True,
    }
    if pip_factor:
        record["atr_pips"] = round(atr_value * pip_factor, 1)
        record["unit"] = "pips"
    else:
        record["atr_points"] = round(atr_value, 2)
        record["unit"] = "points"
    return record


def fetch_daily_atr() -> list[dict[str, Any]]:
    _configure_yfinance_cache()
    cached = _load_cache()
    if cached is not None:
        print("[ATR] Returning cached data")
        return cached
    fallback_cache = _load_any_cache() or []

    print("[ATR] Fetching daily ATR data...")
    ticker_to_symbols: dict[str, list[str]] = {}
    for symbol, ticker in ALL_TICKERS.items():
        ticker_to_symbols.setdefault(ticker, []).append(symbol)

    rows: list[dict[str, Any]] = []
    tickers = list(ticker_to_symbols.keys())
    batch_size = 10
    for idx in range(0, len(tickers), batch_size):
        batch = tickers[idx:idx + batch_size]
        batch_str = " ".join(batch)
        try:
            df = yf.download(
                batch_str,
                period="6mo",
                interval="1d",
                group_by="ticker",
                progress=False,
                threads=False,
                auto_adjust=False,
            )
        except Exception as exc:
            print(f"[ATR] Batch download error: {exc}")
            continue

        for ticker in batch:
            try:
                if len(batch) == 1:
                    frame = df[["High", "Low", "Close"]]
                else:
                    if ticker not in df.columns.get_level_values(0):
                        continue
                    frame = df[ticker][["High", "Low", "Close"]]
            except Exception:
                continue

            for symbol in ticker_to_symbols.get(ticker, []):
                record = _format_record(symbol, frame)
                if record:
                    rows.append(record)

    rows.sort(key=lambda item: item.get("symbol", ""))
    if fallback_cache and len(fallback_cache) > len(rows):
        print("[ATR] Using cached fallback because live fetch was partial")
        return fallback_cache
    if rows:
        _save_cache(rows)
    return rows
