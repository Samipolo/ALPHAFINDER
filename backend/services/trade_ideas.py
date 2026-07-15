from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR, DXY, FOREX_PAIRS, FOREX_TICKERS  # noqa: E402
from services.net_utils import disable_dead_proxy_env  # noqa: E402


CACHE_FILE = os.path.join(CACHE_DIR, "trade_ideas.json")
CACHE_TTL_SECONDS = 3600
CACHE_VERSION = 2


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


def _cache_is_fresh(payload: dict[str, Any]) -> bool:
    try:
        as_of = str(payload.get("as_of") or "")
        if not as_of:
            return False
        stamp = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - stamp).total_seconds() < CACHE_TTL_SECONDS
    except Exception:
        return False


def _load_cache() -> list[dict[str, Any]] | None:
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or not _cache_is_fresh(payload):
            return None
        if int(payload.get("version") or 0) != CACHE_VERSION:
            return None
        data = payload.get("data")
        if not isinstance(data, list) or len(data) < len(FOREX_PAIRS):
            return None
        return data
    except Exception:
        return None


def _save_cache(rows: list[dict[str, Any]]) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(
                {"as_of": datetime.now(timezone.utc).isoformat(), "version": CACHE_VERSION, "data": rows},
                handle,
                default=str,
            )
    except Exception:
        pass


def _flatten_series(frame: pd.DataFrame | pd.Series | None) -> pd.Series:
    if frame is None:
        return pd.Series(dtype="float64")
    if isinstance(frame, pd.Series):
        return frame.dropna()
    if getattr(frame, "ndim", 1) > 1:
        return frame.iloc[:, 0].dropna()
    return pd.Series(frame).dropna()


def _extract_symbol_frame(data: pd.DataFrame, ticker: str, columns: list[str]) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        if ticker not in data.columns.get_level_values(0):
            return pd.DataFrame()
        frame = data[ticker]
        keep = [col for col in columns if col in frame.columns]
        return frame[keep].dropna(how="all")
    keep = [col for col in columns if col in data.columns]
    return data[keep].dropna(how="all")


def _pip_factor(symbol: str) -> int:
    return 100 if symbol.endswith("JPY") else 10000


def _fmt_price(symbol: str, value: float | None) -> float | None:
    if value is None:
        return None
    if symbol.endswith("JPY"):
        return round(value, 3)
    return round(value, 5)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _currency_strength_series(
    currency: str, returns_map: dict[str, pd.Series]
) -> tuple[pd.Series | None, str]:
    if currency == "USD":
        series = returns_map.get("DXY")
        return (series.dropna() if series is not None else None, "DXY")

    direct = f"{currency}USD"
    inverse = f"USD{currency}"
    if direct in returns_map:
        return returns_map[direct].dropna(), direct
    if inverse in returns_map:
        return (-returns_map[inverse]).dropna(), inverse
    return None, ""


def _correlation_bundle(
    symbol: str, returns_map: dict[str, pd.Series]
) -> tuple[float | None, str, str]:
    base, quote = FOREX_PAIRS[symbol]
    pair_returns = returns_map.get(symbol)
    if pair_returns is None or pair_returns.empty:
        return None, "", "Unavailable"

    base_strength, base_anchor = _currency_strength_series(base, returns_map)
    quote_strength, quote_anchor = _currency_strength_series(quote, returns_map)
    if base_strength is None or quote_strength is None:
        return None, "", "Unavailable"

    synthetic = base_strength.subtract(quote_strength, fill_value=0.0).dropna()
    joined = pd.concat(
        [pair_returns.rename("pair"), synthetic.rename("synthetic")], axis=1
    ).dropna()
    if len(joined) < 20:
        return None, "", "Unavailable"

    corr = float(joined["pair"].tail(40).corr(joined["synthetic"].tail(40)))
    anchor = f"{base_anchor or base} vs {quote_anchor or quote}"
    if corr >= 0.6:
        label = "Strong alignment"
    elif corr >= 0.3:
        label = "Supportive alignment"
    elif corr <= -0.3:
        label = "Diverging alignment"
    else:
        label = "Loose alignment"
    return round(corr, 3), anchor, label


def _daily_atr(frame: pd.DataFrame) -> tuple[float | None, float | None]:
    if frame.empty or len(frame) < 20:
        return None, None
    high = frame["High"]
    low = frame["Low"]
    close = frame["Close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr14 = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    latest_atr = _safe_float(atr14.iloc[-1])
    latest_close = _safe_float(close.iloc[-1])
    atr_pct = ((latest_atr / latest_close) * 100.0) if latest_atr and latest_close else None
    return latest_atr, atr_pct


def _session_vwap(frame: pd.DataFrame, symbol: str) -> tuple[float | None, str]:
    if frame.empty:
        return None, "unavailable"
    intraday = frame.copy().dropna(subset=["High", "Low", "Close"])
    if intraday.empty:
        return None, "unavailable"

    if getattr(intraday.index, "tz", None) is None:
        intraday.index = intraday.index.tz_localize("UTC")
    else:
        intraday.index = intraday.index.tz_convert("UTC")

    session_date = intraday.index.max().date()
    session = intraday[intraday.index.date == session_date]
    if session.empty:
        session = intraday.tail(24)

    typical_price = (session["High"] + session["Low"] + session["Close"]) / 3.0
    volume = session["Volume"] if "Volume" in session.columns else pd.Series(index=session.index, dtype="float64")
    volume = _flatten_series(volume).reindex(session.index, fill_value=0.0)
    if float(volume.fillna(0.0).sum()) > 0:
        vwap = float((typical_price * volume).sum() / volume.sum())
        return vwap, "volume_vwap"

    # FX intraday from Yahoo carries no reported volume, so we keep a clearly labeled
    # session-weighted price proxy instead of pretending it is exchange-volume VWAP.
    proxy = float(typical_price.expanding().mean().iloc[-1])
    return proxy, "session_proxy"


def _build_trade_plan(
    symbol: str,
    price: float,
    ema50: float,
    ema200: float,
    vwap_value: float,
    atr_value: float,
    corr_value: float | None,
    corr_label: str,
) -> tuple[str, str, float, float, float, float, float, str]:
    trend_up = price > ema50 > ema200
    trend_down = price < ema50 < ema200
    above_vwap = price >= vwap_value
    below_vwap = price <= vwap_value
    corr_support = (corr_value or 0.0) >= 0.25
    corr_risk = (corr_value or 0.0) <= -0.2

    if trend_up and above_vwap:
        bias = "Bullish"
        setup = "VWAP pullback continuation" if abs(price - vwap_value) <= atr_value * 0.35 else "Momentum continuation"
        entry_low = min(vwap_value, ema50)
        entry_high = max(vwap_value, ema50)
        stop = entry_low - atr_value * 0.85
        target1 = entry_high + atr_value * 0.90
        target2 = entry_high + atr_value * 1.75
        confidence = "High" if corr_support else "Medium"
        note = f"Price is above VWAP and both trend EMAs. Correlation is {corr_label.lower()}."
    elif trend_down and below_vwap:
        bias = "Bearish"
        setup = "VWAP rejection continuation" if abs(price - vwap_value) <= atr_value * 0.35 else "Momentum continuation"
        entry_low = min(vwap_value, ema50)
        entry_high = max(vwap_value, ema50)
        stop = entry_high + atr_value * 0.85
        target1 = entry_low - atr_value * 0.90
        target2 = entry_low - atr_value * 1.75
        confidence = "High" if corr_support else "Medium"
        note = f"Price is below VWAP and both trend EMAs. Correlation is {corr_label.lower()}."
    else:
        bias = "Neutral"
        setup = "Wait for VWAP reclaim / breakdown"
        trigger_high = max(vwap_value, ema50, price)
        trigger_low = min(vwap_value, ema50, price)
        entry_low = trigger_low
        entry_high = trigger_high
        stop = trigger_low - atr_value * 0.60
        target1 = trigger_high + atr_value * 0.75
        target2 = trigger_high + atr_value * 1.30
        confidence = "Low" if corr_risk else "Medium"
        note = "Trend and VWAP are not fully aligned yet. Treat this as a trigger map, not an active continuation setup."

    return bias, setup, entry_low, entry_high, stop, target1, target2, f"{confidence} conviction · {note}"


def fetch_daily_trade_ideas() -> list[dict[str, Any]]:
    cached = _load_cache()
    if cached is not None:
        print("[Ideas] Returning cached daily trade ideas")
        return cached

    print("[Ideas] Building daily trade ideas from live data...")
    _configure_yfinance_cache()

    ticker_map = dict(FOREX_TICKERS)
    ticker_map["DXY"] = DXY["DXY"][0]
    tickers = list(dict.fromkeys(ticker_map.values()))

    daily = yf.download(
        " ".join(tickers),
        period="320d",
        interval="1d",
        group_by="ticker",
        progress=False,
        threads=False,
        auto_adjust=False,
    )
    intraday = yf.download(
        " ".join(list(FOREX_TICKERS.values())),
        period="10d",
        interval="60m",
        group_by="ticker",
        progress=False,
        threads=False,
        auto_adjust=False,
    )

    returns_map: dict[str, pd.Series] = {}
    for symbol, ticker in ticker_map.items():
        frame = _extract_symbol_frame(daily, ticker, ["Close"])
        close = _flatten_series(frame.get("Close"))
        if close.empty:
            continue
        returns_map[symbol] = close.pct_change().dropna()

    rows: list[dict[str, Any]] = []
    for symbol, ticker in FOREX_TICKERS.items():
        daily_frame = _extract_symbol_frame(daily, ticker, ["High", "Low", "Close"])
        intraday_frame = _extract_symbol_frame(
            intraday, ticker, ["High", "Low", "Close", "Volume"]
        )
        close = _flatten_series(daily_frame.get("Close"))
        if close.empty or len(close) < 210:
            continue

        price = _safe_float(close.iloc[-1])
        ema50 = _safe_float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = _safe_float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        atr_value, atr_pct = _daily_atr(daily_frame)
        vwap_value, vwap_mode = _session_vwap(intraday_frame, symbol)
        corr_value, corr_anchor, corr_label = _correlation_bundle(symbol, returns_map)

        if None in (price, ema50, ema200, atr_value, vwap_value):
            continue

        bias, setup, entry_low, entry_high, stop, target1, target2, plan_note = _build_trade_plan(
            symbol,
            float(price),
            float(ema50),
            float(ema200),
            float(vwap_value),
            float(atr_value),
            corr_value,
            corr_label,
        )

        price_delta = float(price - vwap_value)
        vwap_distance_atr = round(price_delta / atr_value, 2) if atr_value else 0.0
        rows.append(
            {
                "symbol": symbol,
                "bias": bias,
                "setup": setup,
                "price": _fmt_price(symbol, float(price)),
                "vwap": _fmt_price(symbol, float(vwap_value)),
                "vwap_mode": vwap_mode,
                "ema50": _fmt_price(symbol, float(ema50)),
                "ema200": _fmt_price(symbol, float(ema200)),
                "atr14": _fmt_price(symbol, float(atr_value)),
                "atr_pct": round(float(atr_pct or 0.0), 2),
                "atr_pips": round(float(atr_value) * _pip_factor(symbol), 1),
                "correlation": corr_value,
                "correlation_anchor": corr_anchor,
                "correlation_label": corr_label,
                "entry_low": _fmt_price(symbol, float(entry_low)),
                "entry_high": _fmt_price(symbol, float(entry_high)),
                "stop": _fmt_price(symbol, float(stop)),
                "target1": _fmt_price(symbol, float(target1)),
                "target2": _fmt_price(symbol, float(target2)),
                "vwap_distance_atr": vwap_distance_atr,
                "note": plan_note,
                "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "source": "yfinance daily + intraday OHLC",
                "is_real": True,
            }
        )

    rows.sort(
        key=lambda item: (
            {"Bullish": 0, "Bearish": 1, "Neutral": 2}.get(str(item.get("bias")), 3),
            -abs(float(item.get("vwap_distance_atr") or 0.0)),
            item.get("symbol", ""),
        )
    )
    if rows:
        _save_cache(rows)
    return rows
