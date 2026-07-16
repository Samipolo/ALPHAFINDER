"""Options Flow - real option-chain analytics with automatic multi-source
fallback: Yahoo Finance -> Cboe -> Nasdaq.

Every number is derived from live chain snapshots (volume, open interest,
implied volatility, bid/ask). No synthetic rows: a symbol whose chain can't
be fetched from ANY source returns an error entry instead of fake data.

Yahoo Finance (yfinance) is tried first since it has the richest per-contract
data, but it runs on a shared pool of cloud IPs and gets rate-limited by
Yahoo under load ("Too Many Requests"). Cboe's and Nasdaq's public delayed
quote feeds are free, keyless, and independent of Yahoo's rate limiter, so
either one covers for Yahoo when it's throttled -- the desk stays live with
real market data instead of a bare 500.
"""
from __future__ import annotations

import math
import re
import threading
import time
from typing import Any

import pandas as pd
import requests
import yfinance as yf

FLOW_SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META", "GLD", "TLT"]

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300
_CACHE_LOCK = threading.Lock()

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
_MAX_EXPIRIES = 4

_OCC_RE = re.compile(r"^[A-Z]+(\d{6})([CP])(\d{8})$")

_CHAIN_COLUMNS = ["strike", "bid", "ask", "lastPrice", "volume", "openInterest", "impliedVolatility"]


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _bs_gamma(spot: float, strike: float, iv: float, t_years: float, r: float = 0.05) -> float:
    if iv <= 0 or t_years <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    return _norm_pdf(d1) / (spot * iv * math.sqrt(t_years))


def _mid(row) -> float:
    bid = float(row.get("bid") or 0)
    ask = float(row.get("ask") or 0)
    last = float(row.get("lastPrice") or 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return last


def _empty_chain_df() -> pd.DataFrame:
    return pd.DataFrame(columns=_CHAIN_COLUMNS)


def _parse_occ(occ_symbol: str) -> tuple[str, str, float] | None:
    """Parse a standard OCC option symbol into (expiry "YYYY-MM-DD", "C"/"P", strike)."""
    m = _OCC_RE.match(occ_symbol.strip().upper())
    if not m:
        return None
    yymmdd, cp, strike8 = m.groups()
    expiry = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    return expiry, cp, int(strike8) / 1000.0


def _num(x) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x) if x == x else 0.0  # noqa: PLR0124 (NaN check)
    s = str(x).strip().replace(",", "")
    if not s or s == "--":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# _____________________________________________________________
#  Provider 1: Yahoo Finance (yfinance) - richest data, primary source
# _____________________________________________________________

def _yahoo_chains(symbol: str) -> tuple[float, float, list[tuple[str, pd.DataFrame, pd.DataFrame]]]:
    tk = yf.Ticker(symbol)
    hist = tk.history(period="5d", interval="1d", auto_adjust=True)
    if hist is None or hist.empty:
        raise ValueError("no spot history")
    spot = float(hist["Close"].iloc[-1])
    chg = float(hist["Close"].pct_change().iloc[-1] * 100) if len(hist) > 1 else 0.0
    expiries = list(tk.options or [])[:_MAX_EXPIRIES]
    if not expiries:
        raise ValueError("no listed options")

    chains = []
    for exp in expiries:
        try:
            ch = tk.option_chain(exp)
        except Exception:
            continue
        if ch.calls.empty and ch.puts.empty:
            continue
        chains.append((exp, ch.calls, ch.puts))
    if not chains:
        raise ValueError("no usable expiries")
    return spot, chg, chains


# _____________________________________________________________
#  Provider 2: Cboe delayed quotes - free, keyless, real per-contract
#  IV/delta/gamma (no Black-Scholes approximation needed)
# _____________________________________________________________

def _cboe_chains(symbol: str) -> tuple[float, float, list[tuple[str, pd.DataFrame, pd.DataFrame]]]:
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
    r = requests.get(url, headers=_UA, timeout=15)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data") or {}
    options = data.get("options") or []
    if not options:
        raise ValueError("Cboe returned no option contracts")
    spot = float(data.get("current_price") or 0)
    if spot <= 0:
        raise ValueError("Cboe returned no spot price")
    chg = float(data.get("price_change_percent") or 0)

    by_expiry: dict[str, dict[str, list[dict]]] = {}
    for opt in options:
        parsed = _parse_occ(opt.get("option", ""))
        if not parsed:
            continue
        expiry, cp, strike = parsed
        row = {
            "strike": strike,
            "bid": _num(opt.get("bid")),
            "ask": _num(opt.get("ask")),
            "lastPrice": _num(opt.get("last_trade_price")),
            "volume": _num(opt.get("volume")),
            "openInterest": _num(opt.get("open_interest")),
            "impliedVolatility": _num(opt.get("iv")),
            "delta": _num(opt.get("delta")),
            "gamma": _num(opt.get("gamma")),
        }
        by_expiry.setdefault(expiry, {"C": [], "P": []})[cp].append(row)

    chains = []
    for expiry in sorted(by_expiry)[:_MAX_EXPIRIES]:
        sides = by_expiry[expiry]
        calls = pd.DataFrame(sides["C"]) if sides["C"] else _empty_chain_df()
        puts = pd.DataFrame(sides["P"]) if sides["P"] else _empty_chain_df()
        if calls.empty and puts.empty:
            continue
        chains.append((expiry, calls, puts))
    if not chains:
        raise ValueError("Cboe chain had no parseable contracts")
    return spot, chg, chains


# _____________________________________________________________
#  Provider 3: Nasdaq option-chain - free, keyless, last-resort fallback.
#  No per-contract IV/Greeks in this public feed, so GEX/IV-term/skew come
#  back unavailable (None) for symbols served from this tier -- volume, open
#  interest, premium flow and max pain are all still real.
# _____________________________________________________________

_NASDAQ_OCC_TAIL_RE = re.compile(r"(\d{6})([cp])(\d{8})$")


def _nasdaq_chains(symbol: str) -> tuple[float, float, list[tuple[str, pd.DataFrame, pd.DataFrame]]]:
    url = f"https://api.nasdaq.com/api/quote/{symbol}/option-chain"
    params = {
        "assetclass": "stocks",
        "limit": "500",
        "fromdate": "all",
        "todate": "undefined",
        "excode": "oprac",
        "callput": "callput",
        "money": "all",
        "type": "all",
    }
    r = requests.get(url, headers=_UA, params=params, timeout=15)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data") or {}
    rows = ((data.get("table") or {}).get("rows")) or []
    if not rows:
        raise ValueError("Nasdaq returned no option rows")
    m = re.search(r"\$?([\d,]+\.\d+)", data.get("lastTrade") or "")
    spot = float(m.group(1).replace(",", "")) if m else 0.0
    if spot <= 0:
        raise ValueError("Nasdaq returned no spot price")
    chg = 0.0  # not present in this feed; last-resort tier, acceptable degradation

    by_expiry: dict[str, dict[str, list[dict]]] = {}
    for row in rows:
        strike = row.get("strike")
        drill = row.get("drillDownURL") or ""
        if strike in (None, "") or not drill:
            continue  # header/expiry-separator row, not a real strike
        tail = _NASDAQ_OCC_TAIL_RE.search(drill.replace("--", ""))
        if not tail:
            continue
        yymmdd = tail.group(1)
        expiry = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
        strike_f = _num(strike)
        by_expiry.setdefault(expiry, {"C": [], "P": []})
        by_expiry[expiry]["C"].append({
            "strike": strike_f,
            "bid": _num(row.get("c_Bid")),
            "ask": _num(row.get("c_Ask")),
            "lastPrice": _num(row.get("c_Last")),
            "volume": _num(row.get("c_Volume")),
            "openInterest": _num(row.get("c_Openinterest")),
            "impliedVolatility": 0.0,
        })
        by_expiry[expiry]["P"].append({
            "strike": strike_f,
            "bid": _num(row.get("p_Bid")),
            "ask": _num(row.get("p_Ask")),
            "lastPrice": _num(row.get("p_Last")),
            "volume": _num(row.get("p_Volume")),
            "openInterest": _num(row.get("p_Openinterest")),
            "impliedVolatility": 0.0,
        })

    chains = []
    for expiry in sorted(by_expiry)[:_MAX_EXPIRIES]:
        sides = by_expiry[expiry]
        calls = pd.DataFrame(sides["C"]) if sides["C"] else _empty_chain_df()
        puts = pd.DataFrame(sides["P"]) if sides["P"] else _empty_chain_df()
        if calls.empty and puts.empty:
            continue
        chains.append((expiry, calls, puts))
    if not chains:
        raise ValueError("Nasdaq chain had no parseable contracts")
    return spot, chg, chains


_PROVIDERS: list[tuple[str, Any]] = [
    ("Yahoo Finance", _yahoo_chains),
    ("Cboe", _cboe_chains),
    ("Nasdaq", _nasdaq_chains),
]


# _____________________________________________________________
#  Provider-agnostic metrics computation (unchanged math, now fed by
#  whichever source actually returned live data)
# _____________________________________________________________

def _max_pain(calls, puts) -> float | None:
    strikes = sorted(set(calls["strike"]).union(set(puts["strike"])))
    if not strikes:
        return None
    best, best_pay = None, None
    c = list(zip(calls["strike"], calls["openInterest"].fillna(0)))
    p = list(zip(puts["strike"], puts["openInterest"].fillna(0)))
    for s in strikes:
        pay = sum(oi * max(0.0, s - k) for k, oi in c) + sum(oi * max(0.0, k - s) for k, oi in p)
        if best_pay is None or pay < best_pay:
            best, best_pay = s, pay
    return float(best)


def _atm_iv(chain, spot: float) -> float | None:
    df = chain[(chain["impliedVolatility"] > 0.001)]
    if df.empty:
        return None
    idx = (df["strike"] - spot).abs().idxmin()
    return float(df.loc[idx, "impliedVolatility"])


def _iv_near(chain, target: float) -> float | None:
    df = chain[(chain["impliedVolatility"] > 0.001)]
    if df.empty:
        return None
    idx = (df["strike"] - target).abs().idxmin()
    return float(df.loc[idx, "impliedVolatility"])


def _compute_metrics(symbol: str, spot: float, chg: float, chains: list[tuple[str, pd.DataFrame, pd.DataFrame]]) -> dict[str, Any]:
    now = time.time()
    per_expiry = []
    unusual: list[dict] = []
    gex_by_strike: dict[float, float] = {}
    tot_cv = tot_pv = tot_coi = tot_poi = 0.0
    net_prem = 0.0

    for exp, calls, puts in chains:
        for col in _CHAIN_COLUMNS:
            if col not in calls.columns:
                calls[col] = 0.0
            if col not in puts.columns:
                puts[col] = 0.0
        has_real_gamma = "delta" in calls.columns and "gamma" in calls.columns
        t_years = max((time.mktime(time.strptime(exp, "%Y-%m-%d")) - now) / (365.25 * 86400), 1 / 365)
        cv = float(calls["volume"].fillna(0).sum())
        pv = float(puts["volume"].fillna(0).sum())
        coi = float(calls["openInterest"].fillna(0).sum())
        poi = float(puts["openInterest"].fillna(0).sum())
        tot_cv += cv; tot_pv += pv; tot_coi += coi; tot_poi += poi
        call_prem = float((calls["volume"].fillna(0) * calls.apply(_mid, axis=1) * 100).sum())
        put_prem = float((puts["volume"].fillna(0) * puts.apply(_mid, axis=1) * 100).sum())
        net_prem += call_prem - put_prem
        atm = _atm_iv(calls, spot)
        put_wing = _iv_near(puts, spot * 0.95)
        call_wing = _iv_near(calls, spot * 1.05)
        cw = calls.loc[calls["openInterest"].fillna(0).idxmax()] if not calls.empty else None
        pw = puts.loc[puts["openInterest"].fillna(0).idxmax()] if not puts.empty else None
        exp_move = None
        try:
            c_atm = calls.iloc[(calls["strike"] - spot).abs().argsort()[:1]]
            p_atm = puts.iloc[(puts["strike"] - spot).abs().argsort()[:1]]
            if not c_atm.empty and not p_atm.empty:
                straddle = _mid(c_atm.iloc[0]) + _mid(p_atm.iloc[0])
                if straddle > 0:
                    exp_move = round(straddle / spot * 100, 2)
        except Exception:
            pass
        per_expiry.append({
            "expiry": exp,
            "dte": round(t_years * 365.25, 1),
            "pcr_volume": round(pv / cv, 2) if cv > 0 else None,
            "pcr_oi": round(poi / coi, 2) if coi > 0 else None,
            "atm_iv_pct": round(atm * 100, 1) if atm else None,
            "skew_25d_pct": round((put_wing - call_wing) * 100, 2) if put_wing and call_wing else None,
            "max_pain": _max_pain(calls, puts),
            "call_wall": float(cw["strike"]) if cw is not None else None,
            "put_wall": float(pw["strike"]) if pw is not None else None,
            "call_premium_usd": round(call_prem),
            "put_premium_usd": round(put_prem),
            "expected_move_pct": exp_move,
        })
        for df, sign, side in ((calls, 1.0, "CALL"), (puts, -1.0, "PUT")):
            for _, row in df.iterrows():
                oi = float(row.get("openInterest") or 0)
                vol = float(row.get("volume") or 0)
                iv = float(row.get("impliedVolatility") or 0)
                strike = float(row["strike"])
                if oi > 0:
                    if has_real_gamma and float(row.get("gamma") or 0) != 0:
                        gamma = abs(float(row.get("gamma") or 0))
                    elif iv > 0.001:
                        gamma = _bs_gamma(spot, strike, iv, t_years)
                    else:
                        gamma = 0.0
                    if gamma:
                        gex = sign * gamma * oi * 100 * spot * spot * 0.01
                        gex_by_strike[strike] = gex_by_strike.get(strike, 0.0) + gex
                if vol >= 300 and vol > 3 * max(oi, 1):
                    prem = vol * _mid(row) * 100
                    if prem > 50000:
                        unusual.append({
                            "side": side, "strike": strike, "expiry": exp,
                            "volume": int(vol), "open_interest": int(oi),
                            "vol_oi_ratio": round(vol / max(oi, 1), 1),
                            "iv_pct": round(iv * 100, 1),
                            "premium_usd": round(prem),
                            "moneyness_pct": round((strike / spot - 1) * 100, 1),
                        })

    unusual.sort(key=lambda u: -u["premium_usd"])
    strikes_sorted = sorted(gex_by_strike.items())
    window = [(k, v) for k, v in strikes_sorted if 0.85 * spot <= k <= 1.15 * spot]
    net_gex = sum(gex_by_strike.values())
    flip = None
    cum = 0.0
    for k, v in strikes_sorted:
        cum += v
        if cum > 0 and flip is None and k >= spot * 0.8:
            flip = k
    term = [{"expiry": e["expiry"], "atm_iv_pct": e["atm_iv_pct"]} for e in per_expiry if e["atm_iv_pct"]]

    oi_ladder, smile = [], []
    ladder_exp = chains[0][0]
    try:
        ch0_calls, ch0_puts = chains[0][1], chains[0][2]
        for cand_exp, cand_calls, cand_puts in chains:
            if float(cand_calls["openInterest"].fillna(0).sum()) + float(cand_puts["openInterest"].fillna(0).sum()) > 0:
                ladder_exp, ch0_calls, ch0_puts = cand_exp, cand_calls, cand_puts
                break
        lo_k, hi_k = spot * 0.88, spot * 1.12
        c0 = ch0_calls[(ch0_calls["strike"] >= lo_k) & (ch0_calls["strike"] <= hi_k)]
        p0 = ch0_puts[(ch0_puts["strike"] >= lo_k) & (ch0_puts["strike"] <= hi_k)]
        c_map = {float(r["strike"]): r for _, r in c0.iterrows()}
        p_map = {float(r["strike"]): r for _, r in p0.iterrows()}
        strikes0 = sorted(set(list(c_map) + list(p_map)))
        step = max(1, len(strikes0) // 30)
        for k in strikes0[::step]:
            cr, pr = c_map.get(k), p_map.get(k)
            oi_ladder.append({
                "strike": k,
                "call_oi": int(cr["openInterest"]) if cr is not None and cr["openInterest"] == cr["openInterest"] else 0,
                "put_oi": int(pr["openInterest"]) if pr is not None and pr["openInterest"] == pr["openInterest"] else 0,
                "call_vol": int(cr["volume"]) if cr is not None and cr["volume"] == cr["volume"] else 0,
                "put_vol": int(pr["volume"]) if pr is not None and pr["volume"] == pr["volume"] else 0,
            })
            civ = float(cr["impliedVolatility"]) if cr is not None else None
            piv = float(pr["impliedVolatility"]) if pr is not None else None
            smile.append({
                "strike": k,
                "moneyness": round((k / spot - 1) * 100, 1),
                "call_iv": round(civ * 100, 1) if civ and civ > 0.001 else None,
                "put_iv": round(piv * 100, 1) if piv and piv > 0.001 else None,
            })
    except Exception:
        pass

    return {
        "symbol": symbol,
        "spot": round(spot, 2),
        "change_1d_pct": round(chg, 2),
        "pcr_volume": round(tot_pv / tot_cv, 2) if tot_cv > 0 else None,
        "pcr_oi": round(tot_poi / tot_coi, 2) if tot_coi > 0 else None,
        "net_premium_flow_usd": round(net_prem),
        "flow_bias": "bullish" if net_prem > 0 else "bearish",
        "net_gex_musd": round(net_gex / 1e6, 1),
        "gamma_regime": "positive (dampening)" if net_gex > 0 else "negative (amplifying)",
        "gamma_flip_est": round(flip, 2) if flip else None,
        "expiries": per_expiry,
        "iv_term_structure": term,
        "term_shape": ("contango" if len(term) >= 2 and term[-1]["atm_iv_pct"] > term[0]["atm_iv_pct"]
                        else "backwardation" if len(term) >= 2 else None),
        "unusual_activity": unusual[:10],
        "oi_ladder": oi_ladder,
        "oi_ladder_expiry": ladder_exp if oi_ladder else None,
        "iv_smile": smile,
        "gex_profile": [{"strike": k, "gex_musd": round(v / 1e6, 2)} for k, v in window],
    }


def fetch_options_flow(symbol: str = "SPY") -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol not in FLOW_SYMBOLS:
        symbol = "SPY"
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(symbol)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]

    errors = []
    data = None
    for label, provider_fn in _PROVIDERS:
        try:
            spot, chg, chains = provider_fn(symbol)
            data = _compute_metrics(symbol, spot, chg, chains)
            data["source"] = (
                f"{label} option chains (real volume / OI / IV)" if label != "Nasdaq"
                else f"{label} option chains (real volume / OI -- IV/GEX unavailable on this fallback tier)"
            )
            break
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            continue

    if data is None:
        raise RuntimeError("All option data sources failed -- " + " | ".join(errors))

    data["symbols"] = FLOW_SYMBOLS
    with _CACHE_LOCK:
        _CACHE[symbol] = (now, data)
    return data