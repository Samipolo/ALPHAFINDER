from __future__ import annotations

import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any

import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR
from services.net_utils import disable_dead_proxy_env


CACHE_FILE = os.path.join(CACHE_DIR, "options_gex.json")
CACHE_TTL = 300

OPTION_UNIVERSE = [
    {"symbol": "SPY", "name": "SPDR S&P 500 ETF", "group": "Index", "proxy": "S&P 500"},
    {"symbol": "QQQ", "name": "Invesco QQQ", "group": "Index", "proxy": "Nasdaq 100"},
    {"symbol": "IWM", "name": "iShares Russell 2000 ETF", "group": "Index", "proxy": "Russell 2000"},
    {"symbol": "DIA", "name": "SPDR Dow Jones ETF", "group": "Index", "proxy": "Dow Jones"},
    {"symbol": "AAPL", "name": "Apple", "group": "Equity", "proxy": "US Mega Cap"},
    {"symbol": "NVDA", "name": "NVIDIA", "group": "Equity", "proxy": "Semiconductor Leader"},
    {"symbol": "TSLA", "name": "Tesla", "group": "Equity", "proxy": "High Beta Equity"},
    {"symbol": "GLD", "name": "SPDR Gold Shares", "group": "Commodity", "proxy": "Gold"},
    {"symbol": "USO", "name": "United States Oil Fund", "group": "Commodity", "proxy": "WTI Crude"},
    {"symbol": "BITO", "name": "ProShares Bitcoin ETF", "group": "Crypto Proxy", "proxy": "Bitcoin"},
    {"symbol": "FXE", "name": "Invesco CurrencyShares Euro", "group": "FX Proxy", "proxy": "EUR/USD"},
    {"symbol": "FXY", "name": "Invesco CurrencyShares Yen", "group": "FX Proxy", "proxy": "JPY / USD"},
    {"symbol": "FXB", "name": "Invesco CurrencyShares Pound", "group": "FX Proxy", "proxy": "GBP/USD"},
    {"symbol": "UUP", "name": "Invesco DB US Dollar Index", "group": "FX Proxy", "proxy": "Dollar Index"},
]


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


def _load_cache() -> list[dict[str, Any]] | None:
    try:
        if os.path.exists(CACHE_FILE):
            if time.time() - os.path.getmtime(CACHE_FILE) < CACHE_TTL:
                with open(CACHE_FILE, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if isinstance(payload, dict):
                    data = payload.get("data")
                    cached = data if isinstance(data, list) else None
                else:
                    cached = payload if isinstance(payload, list) else None
                if isinstance(cached, list) and cached:
                    first = cached[0] if isinstance(cached[0], dict) else {}
                    if "strike_profile" not in first:
                        return None
                return cached
    except Exception:
        pass
    return None


def _save_cache(data: list[dict[str, Any]]) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "as_of": datetime.now().isoformat(),
                    "data": data,
                },
                handle,
                default=str,
            )
    except Exception:
        pass


def _std_norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _std_norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_gamma(spot: float, strike: float, t: float, iv: float, rate: float = 0.01) -> float:
    if spot <= 0 or strike <= 0 or t <= 0 or iv <= 0:
        return 0.0
    sigma_sqrt_t = iv * math.sqrt(t)
    if sigma_sqrt_t <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t) / sigma_sqrt_t
    return _std_norm_pdf(d1) / (spot * sigma_sqrt_t)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _expiry_days(expiry: str) -> int:
    try:
        return max(0, (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days)
    except ValueError:
        return 0


def _price_snapshot(ticker: yf.Ticker) -> tuple[float, float]:
    hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
    close = hist["Close"].dropna() if "Close" in hist else []
    if len(close) == 0:
        return 0.0, 0.0
    spot = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) >= 2 else spot
    change_pct = ((spot - prev) / prev * 100.0) if prev else 0.0
    return spot, change_pct


def _max_pain(calls_df, puts_df, strikes: list[float]) -> float | None:
    best_strike = None
    best_value = None
    for candidate in strikes:
        call_pain = ((calls_df["strike"] - candidate).clip(lower=0) * calls_df["openInterest"].fillna(0)).sum()
        put_pain = ((candidate - puts_df["strike"]).clip(lower=0) * puts_df["openInterest"].fillna(0)).sum()
        total = float(call_pain + put_pain)
        if best_value is None or total < best_value:
            best_value = total
            best_strike = float(candidate)
    return best_strike


def _contract_gex(records, spot: float, t: float, sign: int) -> tuple[float, float, float, list[tuple[float, float]]]:
    total_gex = 0.0
    total_oi = 0.0
    total_volume = 0.0
    strike_rows: list[tuple[float, float]] = []

    if records is None or records.empty or spot <= 0 or t <= 0:
        return total_gex, total_oi, total_volume, strike_rows

    for _, row in records.iterrows():
        strike = _safe_float(row.get("strike"))
        oi = _safe_float(row.get("openInterest"))
        volume = _safe_float(row.get("volume"))
        iv = _safe_float(row.get("impliedVolatility"))
        if strike <= 0 or oi <= 0 or iv <= 0:
            continue
        gamma = _bs_gamma(spot, strike, t, iv)
        gex = sign * gamma * oi * 100.0 * spot * spot * 0.01
        total_gex += gex
        total_oi += oi
        total_volume += volume
        strike_rows.append((strike, gex))

    return total_gex, total_oi, total_volume, strike_rows


def _format_gex(value: float) -> float:
    return round(value / 1_000_000.0, 2)


def _build_strike_profile(
    strike_exposure: dict[float, float],
    spot: float,
    call_wall: float | None,
    put_wall: float | None,
    max_pain: float | None,
) -> list[dict[str, float]]:
    if not strike_exposure:
        return []

    reference_levels = [spot]
    if call_wall is not None:
        reference_levels.append(call_wall)
    if put_wall is not None:
        reference_levels.append(put_wall)
    if max_pain is not None:
        reference_levels.append(max_pain)

    center = sum(reference_levels) / len(reference_levels)
    ranked = sorted(
        strike_exposure.items(),
        key=lambda item: (abs(item[0] - center), -abs(item[1])),
    )
    # Keep a much deeper real strike ladder so the frontend can render a detailed
    # profile instead of a compressed thumbnail view.
    selected = ranked[:36]
    selected.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "strike": round(float(strike), 2),
            "gex": _format_gex(gex),
        }
        for strike, gex in selected
    ]


def _is_real_chart_row(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("is_real") is False or row.get("fallback_used"):
        return False
    if row.get("source") != "Yahoo Finance options chain via yfinance":
        return False
    if not row.get("front_expiry"):
        return False
    price = _safe_float(row.get("price"))
    if price <= 0:
        return False
    profile = row.get("strike_profile")
    if not isinstance(profile, list) or not profile:
        return False
    return any(
        isinstance(item, dict)
        and math.isfinite(_safe_float(item.get("strike")))
        and math.isfinite(_safe_float(item.get("gex")))
        for item in profile
    )


def _analyze_symbol(asset: dict[str, str]) -> dict[str, Any] | None:
    symbol = asset["symbol"]
    ticker = yf.Ticker(symbol)
    expiries = list(ticker.options or [])
    if not expiries:
        return None

    chosen_expiries: list[str] = []
    for expiry in expiries:
        days = _expiry_days(expiry)
        if days > 45:
            continue
        chosen_expiries.append(expiry)
        if len(chosen_expiries) >= 3:
            break
    if not chosen_expiries:
        chosen_expiries = list(expiries[:1])

    spot, change_pct = _price_snapshot(ticker)
    if spot <= 0:
        return None
    fetched_at = datetime.utcnow().isoformat() + "Z"

    total_call_gex = 0.0
    total_put_gex = 0.0
    total_call_oi = 0.0
    total_put_oi = 0.0
    total_call_volume = 0.0
    total_put_volume = 0.0
    strike_exposure: dict[float, float] = {}
    strike_pool: set[float] = set()

    for expiry in chosen_expiries:
        chain = ticker.option_chain(expiry)
        days = max(_expiry_days(expiry), 1)
        t = days / 365.0

        call_gex, call_oi, call_volume, call_rows = _contract_gex(chain.calls, spot, t, 1)
        put_gex, put_oi, put_volume, put_rows = _contract_gex(chain.puts, spot, t, -1)

        total_call_gex += call_gex
        total_put_gex += put_gex
        total_call_oi += call_oi
        total_put_oi += put_oi
        total_call_volume += call_volume
        total_put_volume += put_volume

        for strike, gex in call_rows + put_rows:
            strike_exposure[strike] = strike_exposure.get(strike, 0.0) + gex
            strike_pool.add(strike)

    near_expiry = chosen_expiries[0]
    near_chain = ticker.option_chain(near_expiry)
    strikes = sorted({float(x) for x in strike_pool})[:]
    max_pain = _max_pain(near_chain.calls, near_chain.puts, strikes) if strikes else None

    call_wall = None
    if near_chain.calls is not None and not near_chain.calls.empty:
        call_wall = float(
            near_chain.calls.sort_values(by="openInterest", ascending=False).iloc[0]["strike"]
        )
    put_wall = None
    if near_chain.puts is not None and not near_chain.puts.empty:
        put_wall = float(
            near_chain.puts.sort_values(by="openInterest", ascending=False).iloc[0]["strike"]
        )

    net_gex = total_call_gex + total_put_gex
    put_call_oi = (total_put_oi / total_call_oi) if total_call_oi else None
    dealer_regime = (
        "Positive Gamma"
        if net_gex >= 0
        else "Negative Gamma"
    )

    top_strikes = sorted(
        strike_exposure.items(),
        key=lambda item: abs(item[1]),
        reverse=True,
    )[:3]
    strike_profile = _build_strike_profile(
        strike_exposure,
        spot=spot,
        call_wall=call_wall,
        put_wall=put_wall,
        max_pain=max_pain,
    )

    return {
        "symbol": symbol,
        "name": asset["name"],
        "group": asset["group"],
        "proxy": asset["proxy"],
        "price": round(spot, 4),
        "change_pct": round(change_pct, 2),
        "expiries": chosen_expiries,
        "expiries_available": len(expiries),
        "front_expiry": near_expiry,
        "call_gex": _format_gex(total_call_gex),
        "put_gex": _format_gex(total_put_gex),
        "net_gex": _format_gex(net_gex),
        "call_oi": int(total_call_oi),
        "put_oi": int(total_put_oi),
        "call_volume": int(total_call_volume),
        "put_volume": int(total_put_volume),
        "put_call_oi": round(put_call_oi, 2) if put_call_oi is not None else None,
        "max_pain": round(max_pain, 2) if max_pain is not None else None,
        "call_wall": round(call_wall, 2) if call_wall is not None else None,
        "put_wall": round(put_wall, 2) if put_wall is not None else None,
        "dealer_regime": dealer_regime,
        "top_strikes": [
            {"strike": round(strike, 2), "gex": _format_gex(gex)}
            for strike, gex in top_strikes
        ],
        "strike_profile": strike_profile,
        "source": "Yahoo Finance options chain via yfinance",
        "method": "Approx. gamma exposure from public option chain OI and implied volatility",
        "as_of": fetched_at,
        "is_real": True,
        "fallback_used": False,
    }


def fetch_options_gex() -> list[dict[str, Any]]:
    _configure_yfinance_cache()
    cached = _load_cache()
    if cached is not None:
        real_cached = [row for row in cached if _is_real_chart_row(row)]
        if real_cached:
            print("[OptionsGEX] Returning cached data")
            return real_cached

    print("[OptionsGEX] Fetching option chains...")
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_map = {
            executor.submit(_analyze_symbol, asset): asset["symbol"]
            for asset in OPTION_UNIVERSE
        }
        for future in as_completed(future_map):
            try:
                item = future.result()
                if item and _is_real_chart_row(item):
                    rows.append(item)
            except Exception as exc:
                print(f"[OptionsGEX] {future_map[future]} error: {exc}")

    rows.sort(key=lambda item: abs(float(item.get("net_gex") or 0.0)), reverse=True)
    if rows:
        _save_cache(rows)
    return rows
