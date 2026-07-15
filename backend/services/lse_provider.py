from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import CACHE_DIR  # noqa: E402
from services.net_utils import build_session, disable_dead_proxy_env  # noqa: E402


try:
    from lse import LSE, LSEError
except Exception:  # pragma: no cover - optional dependency
    LSE = None  # type: ignore[assignment]
    LSEError = Exception  # type: ignore[assignment]


CACHE_FILE = os.path.join(CACHE_DIR, "lse_provider.json")
CACHE_TTL = 300

LIVE_WATCHLIST: list[dict[str, Any]] = [
    {"label": "EUR/USD", "aliases": ["EUR/USD", "EURUSD", "EUR USD"], "kind": "forex"},
    {"label": "GBP/USD", "aliases": ["GBP/USD", "GBPUSD", "GBP USD"], "kind": "forex"},
    {"label": "USD/JPY", "aliases": ["USD/JPY", "USDJPY", "USD JPY"], "kind": "forex"},
    {"label": "AUD/USD", "aliases": ["AUD/USD", "AUDUSD", "AUD USD"], "kind": "forex"},
    {"label": "XAU/USD", "aliases": ["XAU/USD", "XAUUSD", "XAU USD", "GOLD"], "kind": "commodity"},
    {"label": "BTC/USD", "aliases": ["BTC/USD", "BTCUSD", "BTC USD"], "kind": "crypto"},
    {"label": "ETH/USD", "aliases": ["ETH/USD", "ETHUSD", "ETH USD"], "kind": "crypto"},
    {"label": "SPX500", "aliases": ["SPX500", "SPX", "S&P 500", "^GSPC"], "kind": "index"},
    {"label": "NAS100", "aliases": ["NAS100", "NASDAQ 100", "NDX", "^NDX"], "kind": "index"},
    {"label": "US30", "aliases": ["US30", "DJIA", "DOW", "^DJI"], "kind": "index"},
    {"label": "SPY", "aliases": ["SPY", "S&P 500 ETF"], "kind": "etf"},
    {"label": "QQQ", "aliases": ["QQQ", "NASDAQ 100 ETF"], "kind": "etf"},
    {"label": "AAPL", "aliases": ["AAPL"], "kind": "stock"},
    {"label": "NVDA", "aliases": ["NVDA"], "kind": "stock"},
    {"label": "TSLA", "aliases": ["TSLA"], "kind": "stock"},
    {"label": "USOIL", "aliases": ["USOIL", "WTI", "CL", "BCO"], "kind": "commodity"},
    {"label": "DXY", "aliases": ["DXY", "USD INDEX", "DOLLAR INDEX"], "kind": "index"},
]

REGION_BY_CCY = {
    "USD": "US",
    "EUR": "EU",
    "GBP": "GB",
    "JPY": "JP",
    "AUD": "AU",
    "NZD": "NZ",
    "CAD": "CA",
    "CHF": "CH",
}

EQUITY_SYMBOLS = {"SPY", "QQQ", "AAPL", "NVDA", "TSLA"}


def _error_payload(exc: Exception) -> dict[str, str]:
    return {
        "type": exc.__class__.__name__,
        "message": str(exc) or exc.__class__.__name__,
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _cache_is_fresh(payload: dict[str, Any]) -> bool:
    try:
        as_of = str(payload.get("as_of") or "")
        if not as_of:
            return False
        stamp = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        return (_now_utc() - stamp).total_seconds() < CACHE_TTL
    except Exception:
        return False


def _load_cache() -> dict[str, Any] | None:
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or not _cache_is_fresh(payload):
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_cache(data: dict[str, Any]) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump({"as_of": _utc_now_iso(), "data": data}, handle, default=str)
    except Exception:
        pass


def _lse_api_key() -> str:
    for key in ("LSE_API_KEY", "LSE_LIVE_API_KEY", "LSE_DATA_API_KEY"):
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def _brue_api_key() -> str:
    for key in ("LSE_BRUE_API_KEY", "LSE_PAPER_API_KEY"):
        value = (os.getenv(key) or "").strip()
        if value.isdigit() and len(value) == 12:
            return value
    return ""


def _build_client(api_key: str | None = None):
    if LSE is None:
        raise RuntimeError("lse-data is not installed")
    return LSE(api_key=api_key or "anonymous")


def _norm(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _as_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows).copy()
    rename_map = {}
    for key in ("o", "open"):
        if key in frame.columns:
            rename_map[key] = "open"
            break
    for key in ("h", "high"):
        if key in frame.columns:
            rename_map[key] = "high"
            break
    for key in ("l", "low"):
        if key in frame.columns:
            rename_map[key] = "low"
            break
    for key in ("c", "close", "last"):
        if key in frame.columns:
            rename_map[key] = "close"
            break
    for key in ("v", "volume"):
        if key in frame.columns:
            rename_map[key] = "volume"
            break
    if rename_map:
        frame = frame.rename(columns=rename_map)
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame = frame.sort_values("timestamp")
    for col in ("open", "high", "low", "close", "volume"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if "close" in frame.columns:
        frame = frame.dropna(subset=["close"])
    return frame.reset_index(drop=True)


def _rsi(series: pd.Series, period: int = 14) -> float | None:
    if series.empty or len(series) <= period:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    value = _to_float(rsi.iloc[-1])
    if value is None:
        return None
    return round(value, 2)


def _resolve_watchlist(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    for item in catalog:
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        index.append(
            {
                "symbol": symbol,
                "name": str(item.get("name") or ""),
                "category": str(item.get("category") or ""),
                "norm_symbol": _norm(symbol),
                "norm_name": _norm(str(item.get("name") or "")),
            }
        )

    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for template in LIVE_WATCHLIST:
        match = None
        for alias in template["aliases"]:
            alias_norm = _norm(alias)
            for item in index:
                if alias_norm == item["norm_symbol"] or alias_norm == item["norm_name"]:
                    match = item["symbol"]
                    break
            if match:
                break
            for item in index:
                if alias_norm and (
                    alias_norm in item["norm_symbol"]
                    or item["norm_symbol"] in alias_norm
                    or alias_norm in item["norm_name"]
                    or item["norm_name"] in alias_norm
                ):
                    match = item["symbol"]
                    break
            if match:
                break
        if match and match not in seen:
            seen.add(match)
            resolved.append(
                {
                    "label": template["label"],
                    "symbol": match,
                    "kind": template["kind"],
                }
            )

    return resolved


def _calendar_regions(label: str, symbol: str) -> list[str]:
    if symbol in {"DXY", "SPY", "QQQ", "AAPL", "NVDA", "TSLA", "SPX500", "NAS100", "US30", "USOIL", "BTC/USD", "ETH/USD"}:
        return ["US"]
    if label.startswith("EUR/"):
        return ["EU", "US"]
    if label.startswith("GBP/"):
        return ["GB", "US"]
    if label.startswith("USD/JPY"):
        return ["JP", "US"]
    if label.startswith("AUD/"):
        return ["AU", "US"]
    if label.startswith("XAU/") or label.startswith("BTC/") or label.startswith("ETH/"):
        return ["US"]
    return ["US"]


def _asset_summary(symbol_row: dict[str, Any], daily: pd.DataFrame, intraday: pd.DataFrame | None = None) -> dict[str, Any]:
    if daily.empty or "close" not in daily.columns:
        return {
            "symbol": symbol_row["symbol"],
            "label": symbol_row["label"],
            "kind": symbol_row["kind"],
            "available": False,
        }

    closes = daily["close"].dropna()
    highs = daily["high"].dropna() if "high" in daily.columns else pd.Series(dtype="float64")
    lows = daily["low"].dropna() if "low" in daily.columns else pd.Series(dtype="float64")
    volumes = daily["volume"].dropna() if "volume" in daily.columns else pd.Series(dtype="float64")
    latest = daily.iloc[-1]
    last_close = _to_float(latest.get("close"))
    prev_close = _to_float(daily.iloc[-2]["close"]) if len(daily) >= 2 else None
    chg_1d = ((last_close / prev_close) - 1.0) * 100.0 if last_close and prev_close else None
    chg_5d = ((last_close / _to_float(closes.iloc[-6])) - 1.0) * 100.0 if last_close and len(closes) >= 6 else None
    chg_20d = ((last_close / _to_float(closes.iloc[-21])) - 1.0) * 100.0 if last_close and len(closes) >= 21 else None
    ema20 = _to_float(closes.ewm(span=20, adjust=False).mean().iloc[-1]) if len(closes) >= 20 else last_close
    ema50 = _to_float(closes.ewm(span=50, adjust=False).mean().iloc[-1]) if len(closes) >= 50 else ema20
    ema200 = _to_float(closes.ewm(span=200, adjust=False).mean().iloc[-1]) if len(closes) >= 200 else ema50
    rsi14 = _rsi(closes, 14)
    vol_ratio = None
    if len(volumes) >= 5 and _to_float(latest.get("volume")) is not None:
        avg_vol = _to_float(volumes.tail(20).mean()) if len(volumes) >= 20 else _to_float(volumes.mean())
        if avg_vol and avg_vol > 0:
            vol_ratio = round(float(latest.get("volume")) / avg_vol, 2)

    atr14 = None
    if {"high", "low", "close"}.issubset(daily.columns) and len(daily) >= 20:
        prev_close_series = daily["close"].shift(1)
        tr = pd.concat(
            [
                (daily["high"] - daily["low"]).abs(),
                (daily["high"] - prev_close_series).abs(),
                (daily["low"] - prev_close_series).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr14 = _to_float(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])

    trend_score = 50
    if last_close and ema20:
        trend_score += 8 if last_close >= ema20 else -8
    if ema20 and ema50:
        trend_score += 8 if ema20 >= ema50 else -8
    if ema50 and ema200:
        trend_score += 6 if ema50 >= ema200 else -6
    if chg_5d is not None:
        trend_score += 6 if chg_5d >= 0 else -6
    if chg_20d is not None:
        trend_score += 6 if chg_20d >= 0 else -6
    if rsi14 is not None:
        trend_score += 4 if rsi14 >= 60 else -4 if rsi14 <= 40 else 0
    if vol_ratio is not None:
        trend_score += 4 if vol_ratio >= 1.5 else 2 if vol_ratio >= 1.1 else 0
    trend_score = max(0, min(100, trend_score))

    if trend_score >= 70:
        setup = "Momentum continuation"
        bias = "Bullish"
    elif trend_score <= 30:
        setup = "Mean reversion / fade"
        bias = "Bearish"
    elif trend_score >= 55:
        setup = "Trend continuation"
        bias = "Bullish"
    elif trend_score <= 45:
        setup = "Range fade / short pullback"
        bias = "Bearish"
    else:
        setup = "Wait for confirmation"
        bias = "Neutral"

    live_price = last_close
    live_change = chg_1d
    live_timestamp = None
    if intraday is not None and not intraday.empty and "close" in intraday.columns:
        latest_intra = intraday.iloc[-1]
        live_price = _to_float(latest_intra.get("close")) or live_price
        if len(intraday) >= 2:
            prev_intra = _to_float(intraday.iloc[-2].get("close"))
            if live_price is not None and prev_intra:
                live_change = ((live_price / prev_intra) - 1.0) * 100.0
        if "timestamp" in intraday.columns:
            live_timestamp = str(latest_intra.get("timestamp") or "")

    summary = {
        "symbol": symbol_row["symbol"],
        "label": symbol_row["label"],
        "kind": symbol_row["kind"],
        "asset_class": symbol_row["kind"],
        "available": True,
        "price": round(float(last_close), 5) if last_close is not None else None,
        "live_price": round(float(live_price), 5) if live_price is not None else None,
        "change_1d_pct": round(float(chg_1d), 2) if chg_1d is not None else None,
        "change_5d_pct": round(float(chg_5d), 2) if chg_5d is not None else None,
        "change_20d_pct": round(float(chg_20d), 2) if chg_20d is not None else None,
        "ema20": round(float(ema20), 5) if ema20 is not None else None,
        "ema50": round(float(ema50), 5) if ema50 is not None else None,
        "ema200": round(float(ema200), 5) if ema200 is not None else None,
        "rsi14": rsi14,
        "atr14": round(float(atr14), 5) if atr14 is not None else None,
        "vol_ratio": vol_ratio,
        "trend_score": trend_score,
        "bias": bias,
        "setup": setup,
        "last_candle_ts": str(latest.get("timestamp") or ""),
        "live_ts": live_timestamp,
        "calendar_regions": _calendar_regions(symbol_row["label"], symbol_row["symbol"]),
    }
    if live_change is not None:
        summary["live_change_pct"] = round(float(live_change), 2)
    if {"high", "low", "close"}.issubset(daily.columns) and last_close is not None:
        summary["range_pct"] = round(float((daily.iloc[-1]["high"] - daily.iloc[-1]["low"]) / last_close * 100.0), 2)
    return summary


def _calendar_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "datetime": str(row.get("datetime") or row.get("date") or ""),
        "region": str(row.get("region_code") or row.get("region") or ""),
        "event": str(row.get("event") or row.get("name") or ""),
        "impact": row.get("impact") or row.get("importance") or row.get("level"),
        "actual": row.get("actual"),
        "forecast": row.get("forecast"),
        "previous": row.get("previous"),
        "unit": row.get("unit"),
        "source": "London Strategic Edge economic calendar",
    }


def _insider_item(row: dict[str, Any]) -> dict[str, Any]:
    tx_type = str(row.get("transaction_type") or row.get("type") or "")
    side = "buy" if tx_type.upper().startswith("P") or "PURCHASE" in tx_type.upper() else "sell" if tx_type.upper().startswith("S") or "SALE" in tx_type.upper() else "other"
    shares = _to_float(row.get("shares") or row.get("qty") or row.get("quantity")) or 0.0
    price = _to_float(row.get("price")) or _to_float(row.get("transaction_price")) or 0.0
    notional = shares * price if shares and price else None
    return {
        "symbol": str(row.get("symbol") or ""),
        "name": str(row.get("insider_name") or row.get("owner") or row.get("reporting_owner") or ""),
        "transaction_date": str(row.get("transaction_date") or row.get("date") or ""),
        "transaction_type": tx_type,
        "side": side,
        "shares": shares,
        "price": price if price else None,
        "notional": round(float(notional), 2) if notional is not None else None,
        "title": str(row.get("title") or row.get("role") or ""),
        "source": "London Strategic Edge insider trades",
    }


def _dividend_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(row.get("symbol") or ""),
        "effective_date": str(row.get("effective_date") or row.get("ex_date") or row.get("date") or ""),
        "amount": _to_float(row.get("amount") or row.get("dividend") or row.get("cash_amount")),
        "currency": str(row.get("currency") or ""),
        "frequency": str(row.get("frequency") or ""),
        "source": "London Strategic Edge dividends",
    }


def _split_item(row: dict[str, Any]) -> dict[str, Any]:
    ratio = row.get("ratio") or row.get("split_ratio")
    return {
        "symbol": str(row.get("symbol") or ""),
        "effective_date": str(row.get("effective_date") or row.get("date") or ""),
        "ratio": str(ratio or ""),
        "source": "London Strategic Edge splits",
    }


def _paper_trading_state() -> dict[str, Any]:
    key = _brue_api_key()
    if not key:
        return {
            "enabled": False,
            "reason": "Set LSE_BRUE_API_KEY to a 12-digit paper-trading key to enable account, positions and orders data.",
        }

    session = build_session({"X-API-Key": key})
    base = "https://api.londonstrategicedge.com/brue"

    def fetch_json(path: str) -> Any:
        response = session.get(f"{base}/{path}", timeout=20)
        response.raise_for_status()
        return response.json()

    try:
        account = fetch_json("account")
    except Exception as exc:
        return {"enabled": False, "reason": str(exc) or "paper trading unavailable"}

    payload: dict[str, Any] = {"enabled": True, "account": account}
    for name in ("positions", "orders"):
        try:
            payload[name] = fetch_json(name)
        except Exception as exc:
            payload[name] = []
            payload.setdefault("errors", {})[name] = _error_payload(exc)
    return payload


def _fetch_with_client(client, method: str, *args, **kwargs):
    fn = getattr(client, method)
    return fn(*args, **kwargs)


def fetch_lse_provider() -> dict[str, Any]:
    cached = _load_cache()
    if cached is not None:
        return cached

    disable_dead_proxy_env()
    api_key = _lse_api_key()
    payload: dict[str, Any] = {
        "source": "London Strategic Edge (lse-data)",
        "configured": bool(api_key),
        "as_of": _utc_now_iso(),
        "coverage": {},
        "watchlist": [],
        "calendar": {},
        "corporate_actions": {},
        "paper_trading": _paper_trading_state(),
        "errors": {},
    }

    try:
        client = _build_client(api_key)
    except Exception as exc:
        payload["errors"]["client"] = _error_payload(exc)
        return payload

    try:
        catalog = client.catalog()
        payload["coverage"]["catalog_total"] = len(catalog)
        by_category: dict[str, int] = {}
        for item in catalog:
            category = str(item.get("category") or "unknown")
            by_category[category] = by_category.get(category, 0) + 1
        payload["coverage"]["categories"] = dict(sorted(by_category.items(), key=lambda item: (-item[1], item[0])))
        payload["coverage"]["sample"] = [
            {"symbol": str(item.get("symbol") or ""), "name": str(item.get("name") or ""), "category": str(item.get("category") or "")}
            for item in catalog[:12]
        ]
    except Exception as exc:
        payload["errors"]["catalog"] = _error_payload(exc)
        catalog = []

    watchlist = _resolve_watchlist(catalog)
    payload["coverage"]["resolved_watchlist"] = len(watchlist)
    payload["coverage"]["watchlist_labels"] = [row["label"] for row in watchlist]

    if not api_key:
        payload["use_cases"] = {
            "universe_expansion": {
                "catalog_total": payload["coverage"].get("catalog_total", 0),
                "resolved_watchlist": payload["coverage"].get("resolved_watchlist", 0),
                "categories": payload["coverage"].get("categories", {}),
            }
        }
        payload["last_updated"] = _utc_now_iso()
        _save_cache(payload)
        return payload

    # Fetch the deeper historical surfaces in parallel.
    daily_rows: dict[str, list[dict[str, Any]]] = {}
    intraday_rows: dict[str, list[dict[str, Any]]] = {}

    def fetch_daily(item: dict[str, Any]):
        return item["symbol"], client.candles(item["symbol"], timeframe="1d", limit=220, order="asc")

    def fetch_intraday(item: dict[str, Any]):
        return item["symbol"], client.candles(item["symbol"], timeframe="1m", limit=5, order="asc")

    try:
        with ThreadPoolExecutor(max_workers=max(2, min(8, len(watchlist) or 1))) as executor:
            futures = {executor.submit(fetch_daily, item): item for item in watchlist}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    symbol, rows = future.result()
                    daily_rows[symbol] = rows or []
                except Exception as exc:
                    payload["errors"][f"candles_daily_{item['symbol']}"] = _error_payload(exc)

        summaries: list[dict[str, Any]] = []
        for item in watchlist:
            frame = _as_frame(daily_rows.get(item["symbol"], []))
            if frame.empty:
                continue
            summaries.append(_asset_summary(item, frame))

        summaries.sort(
            key=lambda row: (
                -int(row.get("trend_score") or 0),
                -abs(float(row.get("change_20d_pct") or 0.0)),
                str(row.get("label") or ""),
            )
        )

        top_for_intraday = summaries[: min(8, len(summaries))]
        if top_for_intraday:
            with ThreadPoolExecutor(max_workers=max(2, min(6, len(top_for_intraday)))) as executor:
                futures = {
                    executor.submit(fetch_intraday, {"symbol": row["symbol"]}): row
                    for row in top_for_intraday
                }
                for future in as_completed(futures):
                    row = futures[future]
                    try:
                        symbol, rows = future.result()
                        intraday_rows[symbol] = rows or []
                    except Exception as exc:
                        payload["errors"][f"candles_intraday_{row['symbol']}"] = _error_payload(exc)

        enriched: list[dict[str, Any]] = []
        for row in summaries:
            intraday_frame = _as_frame(intraday_rows.get(row["symbol"], []))
            if not intraday_frame.empty:
                row = {**row}
                if "close" in intraday_frame.columns:
                    live_price = _to_float(intraday_frame.iloc[-1].get("close"))
                    if live_price is not None:
                        row["live_price"] = round(float(live_price), 5)
                if len(intraday_frame) >= 2 and "close" in intraday_frame.columns:
                    latest = _to_float(intraday_frame.iloc[-1].get("close"))
                    previous = _to_float(intraday_frame.iloc[-2].get("close"))
                    if latest is not None and previous:
                        row["live_change_pct"] = round(((latest / previous) - 1.0) * 100.0, 2)
                if "timestamp" in intraday_frame.columns:
                    row["live_ts"] = str(intraday_frame.iloc[-1].get("timestamp") or "")
            enriched.append(row)

        payload["watchlist"] = enriched
    except Exception as exc:
        payload["errors"]["watchlist"] = _error_payload(exc)

    # Economic calendar: recent and upcoming high-impact events.
    try:
        calendar = client.economic_calendar(
            region=["US", "EU", "GB", "JP", "AU", "NZ", "CA", "CH"],
            start=(_now_utc() - timedelta(days=2)).strftime("%Y-%m-%d"),
            end=(_now_utc() + timedelta(days=7)).strftime("%Y-%m-%d"),
            order="asc",
            limit=1000,
        )
        calendar_rows = [_calendar_item(row) for row in calendar]
        high_impact = []
        for row in calendar_rows:
            impact = row.get("impact")
            try:
                impact_score = int(impact or 0)
            except (TypeError, ValueError):
                impact_score = 0
            if impact_score >= 3:
                high_impact.append(row)
        payload["calendar"] = {
            "count": len(calendar_rows),
            "high_impact_count": len(high_impact),
            "next_high_impact": high_impact[:8],
            "released_count": len([row for row in calendar_rows if row.get("actual") not in (None, "")]),
            "upcoming_count": len([row for row in calendar_rows if row.get("actual") in (None, "")]),
            "source": "London Strategic Edge economic calendar",
        }
    except Exception as exc:
        payload["errors"]["calendar"] = _error_payload(exc)

    # Insider flow, dividends and splits are most useful on equities / ETFs.
    try:
        insiders = client.insider_trades(
            start=(_now_utc() - timedelta(days=30)).strftime("%Y-%m-%d"),
            end=(_now_utc() + timedelta(days=1)).strftime("%Y-%m-%d"),
            limit=200,
        )
        insider_rows = [_insider_item(row) for row in insiders]
        insider_rows.sort(key=lambda row: (row.get("transaction_date") or "", row.get("notional") or 0), reverse=True)
        payload["corporate_actions"]["insider_trades"] = insider_rows[:40]
    except Exception as exc:
        payload["errors"]["insiders"] = _error_payload(exc)

    try:
        dividends = client.dividends(
            start=(_now_utc() - timedelta(days=90)).strftime("%Y-%m-%d"),
            end=(_now_utc() + timedelta(days=90)).strftime("%Y-%m-%d"),
            limit=100,
        )
        dividend_rows = [_dividend_item(row) for row in dividends]
        dividend_rows.sort(key=lambda row: row.get("effective_date") or "", reverse=True)
        payload["corporate_actions"]["dividends"] = dividend_rows[:40]
    except Exception as exc:
        payload["errors"]["dividends"] = _error_payload(exc)

    try:
        splits = client.splits(
            start=(_now_utc() - timedelta(days=365)).strftime("%Y-%m-%d"),
            end=(_now_utc() + timedelta(days=365)).strftime("%Y-%m-%d"),
            limit=100,
        )
        split_rows = [_split_item(row) for row in splits]
        split_rows.sort(key=lambda row: row.get("effective_date") or "", reverse=True)
        payload["corporate_actions"]["splits"] = split_rows[:40]
    except Exception as exc:
        payload["errors"]["splits"] = _error_payload(exc)

    # A compact market-action ranking that blends trend, momentum and flow.
    ranked = []
    for row in payload.get("watchlist", []):
        score = int(row.get("trend_score") or 0)
        if row.get("bias") == "Bullish":
            score += 5
        elif row.get("bias") == "Bearish":
            score -= 5
        change_1d = _to_float(row.get("change_1d_pct")) or 0.0
        change_5d = _to_float(row.get("change_5d_pct")) or 0.0
        score += int(max(-8, min(8, change_1d / 2)))
        score += int(max(-8, min(8, change_5d / 3)))
        ranked.append({**row, "opportunity_score": max(0, min(100, score))})

    ranked.sort(key=lambda row: (-int(row.get("opportunity_score") or 0), str(row.get("label") or "")))
    payload["watchlist"] = ranked
    payload["opportunities"] = ranked[:10]
    live_confirmation = [row for row in ranked if row.get("bias") in {"Bullish", "Bearish"}]
    event_gate = {
        "high_impact_count": payload.get("calendar", {}).get("high_impact_count", 0),
        "next_high_impact": payload.get("calendar", {}).get("next_high_impact", [])[:8],
    }
    corporate_flow = {
        "insider_count": len(payload["corporate_actions"].get("insider_trades") or []),
        "dividend_count": len(payload["corporate_actions"].get("dividends") or []),
        "split_count": len(payload["corporate_actions"].get("splits") or []),
    }
    payload["use_cases"] = {
        "live_candle_confirmation": {
            "count": len(live_confirmation),
            "leaders": live_confirmation[:5],
        },
        "event_gate": event_gate,
        "universe_expansion": {
            "catalog_total": payload["coverage"].get("catalog_total", 0),
            "resolved_watchlist": payload["coverage"].get("resolved_watchlist", 0),
            "categories": payload["coverage"].get("categories", {}),
        },
        "corporate_flow": corporate_flow,
        "intraday_regime": {
            "leaders": [
                {
                    "symbol": row.get("symbol"),
                    "label": row.get("label"),
                    "trend_score": row.get("trend_score"),
                    "rsi14": row.get("rsi14"),
                    "change_1d_pct": row.get("change_1d_pct"),
                    "vol_ratio": row.get("vol_ratio"),
                }
                for row in ranked[:5]
            ]
        },
        "ranked_board": {
            "top": ranked[:10],
        },
    }
    payload["last_updated"] = _utc_now_iso()

    _save_cache(payload)
    return payload
